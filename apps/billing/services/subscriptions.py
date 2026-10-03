"""
Subscription lifecycle (SRS §19: active, past_due, cancelled).

**A cancellation is a date, not a deletion.** Somebody who cancels on the 2nd
has paid through to the 30th. `cancel` records `cancel_at` and leaves the
subscription live; `expire_due` withdraws entitlements when the date arrives.
Withdrawing them at the click would be taking back something already paid for,
which is both wrong and the fastest route to a chargeback.

**Failure to pay does not withdraw the plan.** An invoice past its due date
moves the subscription to `past_due`, which still confers entitlements. Only
`expire_overdue`, after a stated grace period, takes them away. Cutting
somebody off the hour a card fails loses the customer and the payment.

**A change of plan keeps the subscription.** Cancel-then-start would reset the
period and lose the provider reference, and the customer would receive a
cancellation email for an upgrade.

**Decision D-08** sets the commercial rules, with no proration anywhere:

  * a new subscription or a move to a paid plan gives access at once, without
    waiting for the money — payment is often a bank transfer settled by hand;
  * Free -> paid starts a new period on the day of the change, and the full
    period's invoice is raised for it;
  * paid -> Free waits for the end of the current period (`pending_plan`), the
    paid plan works until then, and its invoice stays owed — no refund;
  * an owner with an overdue invoice may not subscribe or move to a paid plan
    until it is settled, however many times they try.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState
from apps.notifications import events

from ..models import (
    LIVE_STATES,
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionState,
)
from . import entitlements

#: How long a `past_due` subscription keeps its entitlements. Long enough for a
#: person to notice an email and move money in a country where a bank transfer
#: is a normal way to pay a bill (ADR-A15).
GRACE_DAYS = 14


@transaction.atomic
def subscribe(
    *,
    owner: Any,
    plan: Plan,
    actor: Any = None,
    provider: str = "manual",
    provider_ref: str = "",
    starts_on: dt.date | None = None,
) -> Subscription:
    """Put an owner on a plan.

    Refuses an owner who already has a live subscription: that is
    `change_plan`, and letting `subscribe` quietly become an upgrade path would
    mean two ways to change a plan with two sets of events.
    """
    audience = entitlements.audience_of(owner)
    if plan.audience != audience:
        raise InvalidState(f"{plan.name} is a {plan.audience} plan.")
    if not plan.is_active:
        raise InvalidState("That plan is no longer offered.")
    if entitlements.live_subscription(owner) is not None:
        raise Conflict("This account already has a plan. Change it instead of adding another.")
    _require_nothing_overdue(owner)

    start = starts_on or timezone.localdate()
    candidate, company = _columns(owner)

    # The `live_subscription` check above narrows the window; the two partial
    # unique constraints close it. Without this savepoint a double-click on
    # Subscribe returns a 500 to the loser of the race instead of the sentence
    # the reader should get (found in the P19 audit).
    try:
        with transaction.atomic():
            subscription = Subscription.objects.create(
                candidate=candidate,
                company=company,
                plan=plan,
                state=SubscriptionState.ACTIVE,
                started_on=start,
                current_period_start=start,
                current_period_end=next_period_end(start, plan.interval),
                provider=provider,
                provider_ref=provider_ref,
            )
    except IntegrityError:
        raise Conflict(
            "This account already has a plan. Change it instead of adding another."
        ) from None

    audit.record(
        verb="billing.subscription_started",
        actor=actor,
        company=company,
        target=subscription,
        metadata={"plan": plan.code, "provider": provider},
    )
    events.emit(
        "subscription.started",
        subscription,
        payload={
            "subscription_public_id": str(subscription.public_id),
            "plan_code": plan.code,
            "owner": subscription.owner_label,
        },
    )
    return subscription


@transaction.atomic
def change_plan(*, subscription: Subscription, plan: Plan, actor: Any = None) -> Subscription:
    """Move an existing subscription onto a different plan of the same audience.

    Decision D-08: paid -> Free is scheduled for the end of the period rather
    than applied; Free -> paid applies at once and starts a new period today;
    choosing the current plan while a move to Free is scheduled keeps it.
    """
    if not subscription.is_live:
        raise InvalidState("That subscription has ended. Start a new one.")
    if plan.pk == subscription.plan_id:
        if subscription.pending_plan_id is not None:
            return keep_plan(subscription=subscription, actor=actor)
        raise InvalidState("That is already the plan.")
    if plan.audience != subscription.plan.audience:
        raise InvalidState("A plan change cannot cross audiences.")
    if not plan.is_active:
        raise InvalidState("That plan is no longer offered.")

    previous = subscription.plan
    if plan.is_free and not previous.is_free:
        return _schedule_downgrade(subscription=subscription, plan=plan, actor=actor)

    _require_nothing_overdue(subscription.owner)
    subscription.plan = plan
    # A change clears a pending cancellation or downgrade: somebody choosing a
    # new plan has said, unambiguously, that they are staying on it.
    subscription.cancel_at = None
    subscription.pending_plan = None
    fields = ["plan", "cancel_at", "pending_plan", "updated_at"]
    if previous.is_free and not plan.is_free:
        # A new paid period from today, billed in full. No proration: the
        # free days already used are not credited, and nothing is pro-rated.
        today = timezone.localdate()
        subscription.current_period_start = today
        subscription.current_period_end = next_period_end(today, plan.interval)
        fields += ["current_period_start", "current_period_end"]
    subscription.save(update_fields=fields)

    _record_change(subscription=subscription, previous=previous, actor=actor)
    return subscription


@transaction.atomic
def keep_plan(*, subscription: Subscription, actor: Any = None) -> Subscription:
    """Withdraw a scheduled move to another plan; the current plan continues."""
    if subscription.pending_plan_id is None:
        raise InvalidState("No plan change is scheduled.")
    scheduled = subscription.pending_plan
    assert scheduled is not None  # checked by pending_plan_id above
    subscription.pending_plan = None
    subscription.save(update_fields=["pending_plan", "updated_at"])
    audit.record(
        verb="billing.downgrade_withdrawn",
        actor=actor,
        company=subscription.company,
        target=subscription,
        metadata={"plan": subscription.plan.code, "was_moving_to": scheduled.code},
    )
    return subscription


def _schedule_downgrade(*, subscription: Subscription, plan: Plan, actor: Any) -> Subscription:
    """Paid -> Free at the end of the period (D-08). Nothing else changes today.

    It clears a pending cancellation, as any change of plan does: somebody who
    chooses Free has said they are staying, on Free.
    """
    subscription.pending_plan = plan
    subscription.cancel_at = None
    subscription.cancelled_at = None
    subscription.save(update_fields=["pending_plan", "cancel_at", "cancelled_at", "updated_at"])
    audit.record(
        verb="billing.downgrade_scheduled",
        actor=actor,
        company=subscription.company,
        target=subscription,
        metadata={
            "from": subscription.plan.code,
            "to": plan.code,
            "effective_on": subscription.current_period_end.isoformat(),
        },
    )
    return subscription


def apply_pending_plan(*, subscription: Subscription) -> bool:
    """Put a scheduled plan in place. Called at the period end; True if it did."""
    if subscription.pending_plan_id is None:
        return False
    previous = subscription.plan
    scheduled = subscription.pending_plan
    assert scheduled is not None  # checked by pending_plan_id above
    subscription.plan = scheduled
    subscription.pending_plan = None
    subscription.save(update_fields=["plan", "pending_plan", "updated_at"])
    _record_change(subscription=subscription, previous=previous, actor=None, scheduled=True)
    return True


def apply_due_downgrades(*, today: dt.date | None = None) -> int:
    """Apply scheduled plans on subscriptions the renewal run does not roll.

    A `past_due` subscription is not renewed (its grace is measured from the
    period end), but a downgrade it scheduled still takes effect on the date
    it was promised for: the paid plan must not run on into the grace period.
    """
    today = today or timezone.localdate()
    due = Subscription.objects.filter(
        state=SubscriptionState.PAST_DUE,
        pending_plan__isnull=False,
        current_period_end__lte=today,
    )
    applied = 0
    for candidate in due:
        with transaction.atomic():
            subscription = (
                Subscription.objects.select_for_update(of=("self",))
                .filter(pk=candidate.pk, pending_plan__isnull=False)
                .select_related("plan", "pending_plan")
                .first()
            )
            if subscription is not None and apply_pending_plan(subscription=subscription):
                applied += 1
    return applied


def _record_change(
    *, subscription: Subscription, previous: Plan, actor: Any, scheduled: bool = False
) -> None:
    audit.record(
        verb="billing.plan_changed",
        actor=actor,
        company=subscription.company,
        target=subscription,
        metadata={"from": previous.code, "to": subscription.plan.code, "scheduled": scheduled},
    )
    events.emit(
        "subscription.changed",
        subscription,
        payload={
            "subscription_public_id": str(subscription.public_id),
            "plan_code": subscription.plan.code,
            "previous_plan_code": previous.code,
        },
    )


def _require_nothing_overdue(owner: Any) -> None:
    """Refuse a new or upgraded plan while an invoice is overdue (D-08)."""
    from . import invoices

    invoice = invoices.overdue_for(owner)
    if invoice is not None:
        raise InvalidState(
            f"Invoice {invoice.number} was due on {invoice.due_on:%d %b %Y} and is still unpaid. "
            "Settle it first, then choose a plan.",
            code="invoice_overdue",
        )


@transaction.atomic
def cancel(
    *, subscription: Subscription, actor: Any = None, immediately: bool = False
) -> Subscription:
    """Schedule the end of a subscription.

    `immediately` exists for staff correcting a mistake — a plan assigned to
    the wrong company — and is audited as such. A customer's own cancellation
    always runs to the end of the period they paid for.
    """
    if not subscription.is_live:
        raise InvalidState("That subscription has already ended.")

    effective = timezone.localdate() if immediately else subscription.current_period_end
    subscription.cancel_at = effective
    subscription.cancelled_at = timezone.now()
    # Ending the subscription supersedes a scheduled move to another plan.
    subscription.pending_plan = None
    if immediately:
        subscription.state = SubscriptionState.CANCELLED
    subscription.save(
        update_fields=["cancel_at", "cancelled_at", "state", "pending_plan", "updated_at"]
    )

    audit.record(
        verb="billing.subscription_cancelled",
        actor=actor,
        company=subscription.company,
        target=subscription,
        metadata={"effective_on": effective.isoformat(), "immediate": immediately},
    )
    events.emit(
        "subscription.cancelled",
        subscription,
        payload={
            "subscription_public_id": str(subscription.public_id),
            "plan_code": subscription.plan.code,
            "effective_on": effective.isoformat(),
        },
    )
    return subscription


@transaction.atomic
def resume(*, subscription: Subscription, actor: Any = None) -> Subscription:
    """Undo a scheduled cancellation before it takes effect."""
    if subscription.cancel_at is None:
        raise InvalidState("That subscription is not scheduled to end.")
    if not subscription.is_live:
        raise InvalidState("That subscription has already ended.")

    subscription.cancel_at = None
    subscription.cancelled_at = None
    subscription.save(update_fields=["cancel_at", "cancelled_at", "updated_at"])
    audit.record(
        verb="billing.subscription_resumed",
        actor=actor,
        company=subscription.company,
        target=subscription,
    )
    return subscription


@transaction.atomic
def renew(*, subscription: Subscription, actor: Any = None) -> Subscription:
    """Roll the period forward. Called by the nightly task, once per period."""
    if not subscription.is_live:
        raise InvalidState("That subscription has ended.")

    # A scheduled move to Free takes effect as the paid period ends (D-08), so
    # the new period is on the new plan and the renewal raises no invoice.
    apply_pending_plan(subscription=subscription)

    subscription.current_period_start = subscription.current_period_end
    subscription.current_period_end = next_period_end(
        subscription.current_period_start, subscription.plan.interval
    )
    # A trial becomes a paid period. A `past_due` state is left alone: renewal
    # is not payment, and clearing it here restarted the grace clock.
    if subscription.state == SubscriptionState.TRIALING:
        subscription.state = SubscriptionState.ACTIVE
    subscription.save(
        update_fields=["current_period_start", "current_period_end", "state", "updated_at"]
    )

    events.emit(
        "subscription.renewed",
        subscription,
        payload={
            "subscription_public_id": str(subscription.public_id),
            "period_end": subscription.current_period_end.isoformat(),
        },
    )
    return subscription


@transaction.atomic
def mark_past_due(*, subscription: Subscription, invoice_number: str) -> Subscription:
    """An invoice went unpaid. The plan keeps working; the clock starts."""
    if subscription.state != SubscriptionState.ACTIVE:
        return subscription

    subscription.state = SubscriptionState.PAST_DUE
    subscription.save(update_fields=["state", "updated_at"])

    audit.record(
        verb="billing.subscription_past_due",
        company=subscription.company,
        target=subscription,
        metadata={"invoice": invoice_number},
    )
    events.emit(
        "subscription.payment_failed",
        subscription,
        payload={
            "subscription_public_id": str(subscription.public_id),
            "invoice_number": invoice_number,
        },
    )
    return subscription


@transaction.atomic
def expire(*, subscription: Subscription, reason: str) -> Subscription:
    """Withdraw entitlements. The one place a plan is actually taken away."""
    if not subscription.is_live:
        return subscription

    subscription.state = (
        SubscriptionState.CANCELLED if reason == "cancelled" else SubscriptionState.EXPIRED
    )
    subscription.save(update_fields=["state", "updated_at"])

    audit.record(
        verb="billing.subscription_expired",
        company=subscription.company,
        target=subscription,
        metadata={"reason": reason, "plan": subscription.plan.code},
    )
    events.emit(
        "subscription.expired",
        subscription,
        payload={
            "subscription_public_id": str(subscription.public_id),
            "plan_code": subscription.plan.code,
            "reason": reason,
        },
    )
    return subscription


def expire_due(*, today: dt.date | None = None) -> int:
    """End subscriptions whose cancellation date has arrived."""
    today = today or timezone.localdate()
    due = Subscription.objects.filter(state__in=LIVE_STATES, cancel_at__lte=today)

    ended = 0
    for subscription in due:
        expire(subscription=subscription, reason="cancelled")
        ended += 1
    return ended


def expire_overdue(*, today: dt.date | None = None, grace_days: int = GRACE_DAYS) -> int:
    """End subscriptions that have been `past_due` past the grace period.

    Measured from the period end rather than from when the state changed, so a
    subscription that bounced between states cannot accumulate grace by
    failing repeatedly.
    """
    today = today or timezone.localdate()
    cutoff = today - dt.timedelta(days=grace_days)
    overdue = Subscription.objects.filter(
        state=SubscriptionState.PAST_DUE, current_period_end__lt=cutoff
    )
    ended = 0
    for subscription in overdue:
        expire(subscription=subscription, reason="unpaid")
        ended += 1
    return ended


def next_period_end(start: dt.date, interval: str) -> dt.date:
    """One interval after `start`.

    Month arithmetic by hand rather than a dependency: the only hard case is a
    period starting on the 31st, and the answer — the last day of the shorter
    month — is four lines. Somebody who subscribes on 31 January is billed on
    28 February and on 31 March, which is what every provider does.
    """
    if interval == BillingInterval.YEAR:
        try:
            return start.replace(year=start.year + 1)
        except ValueError:  # 29 February
            return start.replace(year=start.year + 1, day=28)

    month = start.month + 1
    year = start.year + (month > 12)
    month = month - 12 if month > 12 else month
    day = min(start.day, _days_in(year, month))
    return dt.date(year, month, day)


def _days_in(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def _columns(owner: Any) -> tuple[Any, Any]:
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company

    if isinstance(owner, Company):
        return None, owner
    if isinstance(owner, CandidateProfile):
        return owner, None

    profile = getattr(owner, "candidate_profile", None)
    if profile is None:
        raise InvalidState("That account has no candidate profile to bill.")
    return profile, None


# --- paying before access (ADR-A59) -----------------------------------------
#
# Choosing a paid plan used to switch it on at once and bill afterwards
# (D-08). It now works the way a customer expects a purchase to work: the plan
# is chosen, an invoice is raised, and the plan starts only when that invoice
# is verified paid, by a gateway's server-side confirmation or by a staff
# member recording a bank transfer. Until then the owner keeps what they had.
#
# The waiting subscription is a row in state `incomplete`. It confers nothing
# (it is not in LIVE_STATES), there is at most one per owner (database
# constraint), and it expires with its invoice if nobody pays.

#: How long an unpaid checkout waits before it is closed.
CHECKOUT_EXPIRES_DAYS = 7


def pending_checkout(owner: Any) -> Subscription | None:
    """The plan this owner has chosen and not yet paid for, if any."""
    candidate, company = _columns(owner)
    query = Subscription.objects.select_related("plan").filter(state=SubscriptionState.INCOMPLETE)
    query = query.filter(candidate=candidate) if candidate else query.filter(company=company)
    return query.first()


@transaction.atomic
def start_checkout(*, owner: Any, plan: Plan, actor: Any = None) -> tuple[Subscription, Any]:
    """Choose a paid plan: an `incomplete` subscription and its invoice.

    Idempotent: choosing the same plan again (a refresh, a second tab, a
    double-click) returns the same checkout and invoice. Choosing a different
    plan replaces an unpaid checkout, unless a payment of it is already being
    confirmed by a gateway.
    """
    from ..models import AttemptState, InvoiceState, PaymentAttempt
    from . import invoices

    audience = entitlements.audience_of(owner)
    if plan.audience != audience:
        raise InvalidState(f"{plan.name} is a {plan.audience} plan.")
    if not plan.is_active:
        raise InvalidState("That plan is no longer offered.")
    if plan.is_free:
        raise InvalidState("A free plan needs no payment.")
    if plan.is_quoted:
        raise InvalidState(f"{plan.name} is priced by agreement. Talk to us to set it up.")
    current = entitlements.live_subscription(owner)
    if current is not None and current.plan_id == plan.pk:
        raise InvalidState(f"You are already on {plan.name}.")
    _require_nothing_overdue(owner)

    existing = pending_checkout(owner)
    if existing is not None:
        existing = Subscription.objects.select_for_update().get(pk=existing.pk)
        if existing.plan_id == plan.pk and existing.state == SubscriptionState.INCOMPLETE:
            invoice = existing.invoices.order_by("-issued_on").first() or invoices.issue(
                subscription=existing, actor=actor
            )
            return existing, invoice
        confirming = PaymentAttempt.objects.filter(
            invoice__subscription=existing, state=AttemptState.PENDING
        ).exists()
        if confirming:
            raise InvalidState(
                f"A payment for {existing.plan.name} is still being confirmed. "
                "Wait for it before choosing another plan."
            )
        abandon_checkout(subscription=existing, actor=actor, reason="replaced")

    candidate, company = _columns(owner)
    today = timezone.localdate()
    try:
        with transaction.atomic():
            subscription = Subscription.objects.create(
                candidate=candidate,
                company=company,
                plan=plan,
                state=SubscriptionState.INCOMPLETE,
                started_on=today,
                current_period_start=today,
                current_period_end=next_period_end(today, plan.interval),
                provider="checkout",
            )
    except IntegrityError:
        # The other tab won: answer with its checkout.
        winner = pending_checkout(owner)
        if winner is None:  # pragma: no cover - a different clash
            raise Conflict("That plan could not be started. Try again.") from None
        return winner, winner.invoices.order_by("-issued_on").first()

    invoice = invoices.issue(subscription=subscription, actor=actor)
    if invoice.state != InvoiceState.OPEN:  # pragma: no cover - a zero total
        activate_paid(invoice=invoice)
    audit.record(
        verb="billing.checkout_started",
        actor=actor,
        company=company,
        target=subscription,
        metadata={"plan": plan.code, "invoice": invoice.number, "total": str(invoice.total)},
    )
    return subscription, invoice


@transaction.atomic
def abandon_checkout(*, subscription: Subscription, actor: Any = None, reason: str = "") -> None:
    """Close an unpaid checkout and void its invoice. Nothing else changes."""
    from ..models import InvoiceState
    from . import invoices

    if subscription.state != SubscriptionState.INCOMPLETE:
        return
    subscription.state = SubscriptionState.EXPIRED
    subscription.save(update_fields=["state", "updated_at"])
    for invoice in subscription.invoices.filter(state=InvoiceState.OPEN):
        invoices.void(invoice=invoice, actor=actor, reason=reason or "checkout abandoned")
    audit.record(
        verb="billing.checkout_closed",
        actor=actor,
        company=subscription.company,
        target=subscription,
        metadata={"plan": subscription.plan.code, "reason": reason},
    )


def activate_paid(*, invoice: Any) -> bool:
    """A checkout's invoice was verified paid: the plan starts now.

    Called inside the settlement transaction, before the invoice is marked
    paid, so the subscription and the invoice change together or not at all.
    The period starts on the day of payment, not the day of choosing, and the
    invoice's period moves with it. The owner's previous live plan ends here:
    there is only ever one live plan.
    """
    subscription = Subscription.objects.select_for_update().get(pk=invoice.subscription_id)
    if subscription.state != SubscriptionState.INCOMPLETE:
        return False

    previous = entitlements.live_subscription(subscription.owner)
    if previous is not None and previous.pk != subscription.pk:
        previous = Subscription.objects.select_for_update().get(pk=previous.pk)
        previous.state = SubscriptionState.CANCELLED
        previous.cancelled_at = timezone.now()
        previous.cancel_at = timezone.localdate()
        previous.pending_plan = None
        previous.save(
            update_fields=["state", "cancelled_at", "cancel_at", "pending_plan", "updated_at"]
        )
        audit.record(
            verb="billing.subscription_superseded",
            actor=None,
            company=previous.company,
            target=previous,
            metadata={"plan": previous.plan.code, "by": subscription.plan.code},
        )

    today = timezone.localdate()
    subscription.state = SubscriptionState.ACTIVE
    subscription.started_on = today
    subscription.current_period_start = today
    subscription.current_period_end = next_period_end(today, subscription.plan.interval)
    subscription.save(
        update_fields=[
            "state",
            "started_on",
            "current_period_start",
            "current_period_end",
            "updated_at",
        ]
    )
    if invoice.period_start != today:
        invoice.period_start = today
        invoice.period_end = subscription.current_period_end
        invoice.save(update_fields=["period_start", "period_end", "updated_at"])

    audit.record(
        verb="billing.subscription_started",
        actor=None,
        company=subscription.company,
        target=subscription,
        metadata={
            "plan": subscription.plan.code,
            "provider": "checkout",
            "invoice": invoice.number,
        },
    )
    events.emit(
        "subscription.started",
        subscription,
        payload={
            "subscription_public_id": str(subscription.public_id),
            "plan_code": subscription.plan.code,
            "owner": subscription.owner_label,
        },
    )
    return True


def expire_unpaid_checkouts(*, today: dt.date | None = None) -> int:
    """Close checkouts nobody paid for within CHECKOUT_EXPIRES_DAYS."""
    from ..models import AttemptState, PaymentAttempt

    today = today or timezone.localdate()
    cutoff = today - dt.timedelta(days=CHECKOUT_EXPIRES_DAYS)
    closed = 0
    for subscription in Subscription.objects.filter(
        state=SubscriptionState.INCOMPLETE, started_on__lte=cutoff
    ):
        if PaymentAttempt.objects.filter(
            invoice__subscription=subscription, state=AttemptState.PENDING
        ).exists():
            continue  # a gateway is still deciding; the reconciler settles it
        abandon_checkout(subscription=subscription, reason="unpaid checkout expired")
        closed += 1
    return closed
