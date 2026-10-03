"""
Plans, subscriptions and what they entitle somebody to (SRS §5.14).

Three decisions shape this module.

**A plan's limits are data, not code.** `Plan.limits` is a JSONB document with
its own schema version (ADR-A27), read through one function in
`services/entitlements.py`. The alternative — a boolean column per feature —
means a migration every time commercial terms move, and commercial terms move
more often than schemas should.

**A subscription belongs to exactly one owner.** A candidate has one and a
company has one, and neither is a special case of the other, so both columns
are nullable with a database `CHECK` that exactly one is set. A "polymorphic
owner" generic key was rejected: these are the two owners the product has, and
a generic key would buy nothing but unindexable joins.

**Nothing here is a rate limit.** `apps/common/ratelimit.py` refuses abuse;
this refuses *unpaid* use. They fail differently, they read differently to the
person who hits them, and conflating them would have a free user told they are
going too fast when what they actually need is a plan.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import PublicIdModel, TimeStampedModel

#: Version of the `Plan.limits` document (ADR-A27). Bumping it means adding an
#: upgrade branch in `entitlements._read_limits`, never editing rows in place.
LIMITS_SCHEMA_VERSION = 1


class PlanAudience(models.TextChoices):
    CANDIDATE = "candidate", _("Candidate")
    COMPANY = "company", _("Company")


class BillingInterval(models.TextChoices):
    MONTH = "month", _("Monthly")
    YEAR = "year", _("Yearly")


class SubscriptionState(models.TextChoices):
    """The states §19's table names, and no others.

    `past_due` is separate from `cancelled` because they mean opposite things
    about intent: one is a payment problem the customer wants fixed, the other
    is a customer who left. Collapsing them would dunning-email people who
    already cancelled.
    """

    #: Chosen and invoiced, not yet paid (ADR-A59). Confers nothing: the
    #: owner keeps their current plan until the invoice is verified paid.
    INCOMPLETE = "incomplete", _("Awaiting payment")
    TRIALING = "trialing", _("Trialing")
    ACTIVE = "active", _("Active")
    PAST_DUE = "past_due", _("Past due")
    CANCELLED = "cancelled", _("Cancelled")
    EXPIRED = "expired", _("Expired")


#: States in which a subscription still confers its plan's entitlements. A
#: `past_due` subscription keeps working: cutting somebody off the hour a card
#: fails loses the customer *and* the payment.
LIVE_STATES = (SubscriptionState.TRIALING, SubscriptionState.ACTIVE, SubscriptionState.PAST_DUE)


class InvoiceState(models.TextChoices):
    DRAFT = "draft", _("Draft")
    OPEN = "open", _("Open")
    PAID = "paid", _("Paid")
    VOID = "void", _("Void")
    UNCOLLECTIBLE = "uncollectible", _("Uncollectible")


class PaymentState(models.TextChoices):
    PENDING = "pending", _("Pending")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED = "failed", _("Failed")
    REFUNDED = "refunded", _("Refunded")


class UsagePeriod(models.TextChoices):
    DAY = "day", _("Daily")
    MONTH = "month", _("Monthly")


class Plan(TimeStampedModel):
    """What somebody is allowed to do, and what it costs.

    `is_default` marks the plan an owner has when they have no subscription at
    all. Every account therefore has a plan — there is no "no plan" branch in
    the entitlement code, which is where a missing-plan bug would otherwise
    live and silently allow everything.
    """

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=60)
    audience = models.CharField(max_length=10, choices=PlanAudience.choices)
    summary = models.CharField(max_length=200, blank=True)

    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="NPR")
    interval = models.CharField(
        max_length=8, choices=BillingInterval.choices, default=BillingInterval.MONTH
    )
    #: True for a plan whose price is negotiated rather than published. The
    #: page says "talk to us"; the column says the same thing to the code, so a
    #: checkout button is never rendered for a price nobody has agreed.
    is_quoted = models.BooleanField(default=False)

    limits = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("audience", "order", "price")
        constraints = [
            models.UniqueConstraint(
                fields=["audience"],
                condition=models.Q(is_default=True),
                name="plan_one_default_per_audience",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_free(self) -> bool:
        return self.price == Decimal("0") and not self.is_quoted


class SubscriptionQuerySet(models.QuerySet):
    def live(self) -> SubscriptionQuerySet:
        """Subscriptions that still confer entitlements."""
        return self.filter(state__in=LIVE_STATES)


class Subscription(PublicIdModel, TimeStampedModel):
    """One owner's plan over one period.

    `provider` and `provider_ref` are on the subscription rather than in a
    provider-specific table because every provider the platform will ever have
    answers the same two questions — who took the money, and what did they call
    this — and a table per provider answers them in three incompatible ways.
    """

    company = models.ForeignKey(
        "companies.Company",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    candidate = models.ForeignKey(
        "candidates.CandidateProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")

    state = models.CharField(
        max_length=10, choices=SubscriptionState.choices, default=SubscriptionState.ACTIVE
    )
    started_on = models.DateField(default=timezone.localdate)
    current_period_start = models.DateField(default=timezone.localdate)
    current_period_end = models.DateField()
    #: When a cancellation takes effect. Set rather than acted on immediately,
    #: because somebody who cancels on the 2nd paid through to the 30th.
    cancel_at = models.DateField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    #: A plan chosen to take over at the end of this period (decision D-08:
    #: Pro -> Free waits for the period end). Applied by `renew`; the current
    #: plan and its invoice stand until then.
    pending_plan = models.ForeignKey(
        Plan, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    provider = models.CharField(max_length=20, default="manual")
    provider_ref = models.CharField(max_length=120, blank=True)

    objects = SubscriptionQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(company__isnull=False, candidate__isnull=True)
                    | models.Q(company__isnull=True, candidate__isnull=False)
                ),
                name="subscription_exactly_one_owner",
            ),
            # One live subscription per owner, enforced by the database. Two
            # would make "which plan am I on" a question with two answers, and
            # the entitlement code would quietly pick whichever sorted first.
            models.UniqueConstraint(
                fields=["candidate"],
                condition=models.Q(state__in=LIVE_STATES, candidate__isnull=False),
                name="subscription_one_live_per_candidate",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(state__in=LIVE_STATES, company__isnull=False),
                name="subscription_one_live_per_company",
            ),
            # One checkout awaiting payment per owner (ADR-A59): two tabs or a
            # double-click on "Move to Pro" land on the same invoice.
            models.UniqueConstraint(
                fields=["candidate"],
                condition=models.Q(state="incomplete", candidate__isnull=False),
                name="subscription_one_checkout_per_candidate",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(state="incomplete", company__isnull=False),
                name="subscription_one_checkout_per_company",
            ),
        ]
        indexes = [
            models.Index(fields=["state", "current_period_end"], name="subscription_renewal_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.owner_label} on {self.plan.code}"

    @property
    def owner(self) -> object:
        return self.company or self.candidate

    @property
    def owner_label(self) -> str:
        if self.company_id:
            return f"company:{self.company_id}"
        return f"candidate:{self.candidate_id}"

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def days_remaining(self) -> int:
        return max((self.current_period_end - timezone.localdate()).days, 0)


class Invoice(PublicIdModel, TimeStampedModel):
    """What was owed for one period.

    Kept even when a subscription is deleted is *not* the design: an invoice
    cascades with its subscription, and the durable record of money is the
    audit log plus the provider's own books. A billing system that is the sole
    record of a transaction is a compliance problem this product does not need.
    """

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="invoices"
    )
    number = models.CharField(max_length=30, unique=True)
    state = models.CharField(
        max_length=14, choices=InvoiceState.choices, default=InvoiceState.DRAFT
    )

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="NPR")

    period_start = models.DateField()
    period_end = models.DateField()
    issued_on = models.DateField(default=timezone.localdate)
    due_on = models.DateField()
    paid_at = models.DateTimeField(null=True, blank=True)

    coupon = models.ForeignKey(
        "billing.Coupon", null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    lines = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ("-issued_on", "-pk")
        constraints = [
            # One invoice per subscription per period, enforced by the database.
            # `invoices.issue` claimed this property with a `filter().first()`
            # check, which two concurrent renewals — or one retried Celery task
            # — both pass before either inserts. The customer is then billed
            # twice for one month, and no amount of care in the service can
            # prevent it without the constraint.
            models.UniqueConstraint(
                fields=["subscription", "period_start"], name="invoice_one_per_period"
            ),
        ]
        indexes = [
            models.Index(fields=["subscription", "-issued_on"], name="invoice_subscription_idx"),
            models.Index(fields=["state", "due_on"], name="invoice_due_idx"),
        ]

    def __str__(self) -> str:
        return self.number

    @property
    def is_overdue(self) -> bool:
        return self.state == InvoiceState.OPEN and self.due_on < timezone.localdate()


class Payment(TimeStampedModel):
    """One attempt to settle an invoice.

    A row per attempt rather than a status on the invoice: a failed card and a
    later successful bank transfer are two facts, and the failure is the one
    support needs to see when the customer says they already paid.

    `provider_ref` is unique per provider, which is what makes a webhook
    replay — the normal behaviour of every payment gateway — land on the row it
    already created instead of taking the money twice.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    provider = models.CharField(max_length=20)
    provider_ref = models.CharField(max_length=120)
    state = models.CharField(
        max_length=10, choices=PaymentState.choices, default=PaymentState.PENDING
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="NPR")
    detail = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Set when a person recorded this by hand rather than a provider reporting it.",
    )

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_ref"], name="payment_one_per_provider_ref"
            )
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.provider_ref} ({self.state})"


class PaymentEnvironment(models.TextChoices):
    TEST = "test", _("Test (sandbox)")
    LIVE = "live", _("Live")


class AttemptState(models.TextChoices):
    #: Created here; the customer has been (or is about to be) sent to the gateway.
    INITIATED = "initiated", _("Initiated")
    #: The gateway has the payment but has not finished it (eSewa PENDING /
    #: AMBIGUOUS, Khalti Pending), or its status service could not be reached.
    PENDING = "pending", _("Awaiting confirmation")
    SUCCEEDED = "succeeded", _("Paid")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")
    EXPIRED = "expired", _("Expired")
    REFUNDED = "refunded", _("Refunded")


#: States an attempt never leaves. A late or replayed callback for one of these
#: is answered from the row, not by asking the gateway to change its mind.
FINAL_ATTEMPT_STATES = frozenset(
    {
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.EXPIRED,
        AttemptState.REFUNDED,
    }
)


class PaymentAttempt(PublicIdModel, TimeStampedModel):
    """One trip to a payment gateway for one invoice (ADR-A55, ADR-A58).

    The row exists *before* the customer leaves the site, and it is what every
    callback is checked against: the amount sent, the invoice it was for and
    the reference the gateway was given. A callback naming a reference this
    table does not have, or an amount it did not send, is refused without
    writing anything. The gateway's own status service, called from the server,
    decides the outcome; the browser's return trip only says "go and ask".

    `Payment` remains the record of money. An attempt that succeeds points at
    the one `Payment` it produced, and a replayed callback finds that pointer
    already set.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="attempts")
    provider = models.CharField(max_length=20)
    environment = models.CharField(
        max_length=4, choices=PaymentEnvironment.choices, default=PaymentEnvironment.TEST
    )
    #: What this site called the payment when it sent the customer away
    #: (eSewa `transaction_uuid`, Khalti `purchase_order_id`).
    reference = models.CharField(max_length=64)
    #: What the gateway called it (Khalti `pidx`). eSewa has none until it pays.
    gateway_token = models.CharField(max_length=120, blank=True)
    #: The gateway's transaction id once paid (eSewa `ref_id`, Khalti `transaction_id`).
    gateway_ref = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="NPR")
    state = models.CharField(
        max_length=10, choices=AttemptState.choices, default=AttemptState.INITIATED
    )
    gateway_status = models.CharField(max_length=40, blank=True)
    failure_reason = models.CharField(max_length=300, blank=True)
    checks = models.PositiveSmallIntegerField(default=0)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    payment = models.OneToOneField(
        Payment, null=True, blank=True, on_delete=models.SET_NULL, related_name="attempt"
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    #: The gateway's answers, as received, minus anything that is a credential.
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "reference"], name="attempt_one_per_reference"
            ),
        ]
        indexes = [
            models.Index(fields=["state", "created_at"], name="attempt_state_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.reference} ({self.state})"

    @property
    def is_final(self) -> bool:
        return self.state in FINAL_ATTEMPT_STATES

    @property
    def provider_label(self) -> str:
        from .gateways import base as gateways

        gateway = gateways.registry().get(self.provider)
        return gateway.label if gateway else self.provider


class Coupon(TimeStampedModel):
    """A discount somebody may redeem.

    Percentage *or* amount, never both: a coupon that is 20% off and 500 off
    has two answers to what it is worth, and whichever the code applies first
    becomes an accidental policy.
    """

    code = models.CharField(max_length=32, unique=True)
    description = models.CharField(max_length=120, blank=True)
    percent_off = models.PositiveSmallIntegerField(null=True, blank=True)
    amount_off = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="NPR")

    plan = models.ForeignKey(
        Plan,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="coupons",
        help_text="Restricts the coupon to one plan. Blank means any plan.",
    )
    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    redemptions = models.PositiveIntegerField(default=0)
    expires_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("code",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(percent_off__isnull=False, amount_off__isnull=True)
                    | models.Q(percent_off__isnull=True, amount_off__isnull=False)
                ),
                name="coupon_percent_or_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(percent_off__isnull=True) | models.Q(percent_off__lte=100),
                name="coupon_percent_within_range",
            ),
        ]

    def __str__(self) -> str:
        return self.code

    @property
    def is_redeemable(self) -> bool:
        if not self.is_active:
            return False
        if self.expires_on is not None and self.expires_on < timezone.localdate():
            return False
        return self.max_redemptions is None or self.redemptions < self.max_redemptions


class UsageCounter(TimeStampedModel):
    """What one owner has used of one metered thing, in one period.

    Generic owner, matching `AIUsageQuota` (§5.11): a user, a candidate profile
    and a company all meter things, and none of them is a special case of the
    others. The unique constraint is what makes `record` an atomic increment
    rather than a read-modify-write two requests can lose.

    Only *metered* limits appear here. A boolean entitlement — may this account
    use pro templates — is answered from the plan and counts nothing.
    """

    owner_ct = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.CASCADE, related_name="+"
    )
    owner_id = models.CharField(max_length=64)

    metric = models.CharField(max_length=40)
    period = models.CharField(max_length=8, choices=UsagePeriod.choices, default=UsagePeriod.MONTH)
    period_start = models.DateField()
    value = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-period_start", "metric")
        constraints = [
            models.UniqueConstraint(
                fields=["owner_ct", "owner_id", "metric", "period", "period_start"],
                name="usagecounter_one_per_period",
            )
        ]
        indexes = [
            models.Index(
                fields=["owner_ct", "owner_id", "-period_start"], name="usagecounter_owner_idx"
            )
        ]

    def __str__(self) -> str:
        return f"{self.owner_id} {self.metric} {self.period_start}: {self.value}"

    @staticmethod
    def start_of(period: str, *, today: dt.date | None = None) -> dt.date:
        """The first day of the period containing `today`."""
        today = today or timezone.localdate()
        return today if period == UsagePeriod.DAY else today.replace(day=1)
