"""
Invoices (SRS §5.14).

**An invoice is raised for a period, once — and the database is what says so.**
`issue` reads first and returns any invoice that already exists, but the read is
an optimisation, not the guarantee: two concurrent renewals, or one Celery task
retried after a timeout, both pass that read before either inserts. The unique
constraint on (subscription, period_start) is what actually prevents a customer
being billed twice for one month; the loser of the race reads back the winner's
invoice, because both callers wanted the same thing.

*(Until the P19 audit this module asserted that property in this docstring and
enforced it with the read alone. The constraint arrived with
`0002_invoice_invoice_one_per_period`.)*

**The number is allocated from the primary key**, after insert. A counter in a
settings row would need its own lock; a random string is not an invoice number
anybody can read down a bank statement. The insert-then-number pair runs inside
one transaction, so a rolled-back invoice consumes a number and no more.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState
from apps.notifications import events

from ..models import Coupon, Invoice, InvoiceState, Subscription

#: How long a customer has to pay before the invoice is overdue. Two weeks,
#: matching the grace period in `subscriptions.GRACE_DAYS` — a due date that
#: arrives after entitlements are withdrawn would be a bill for something
#: already switched off.
DUE_DAYS = 14


@transaction.atomic
def issue(
    *,
    subscription: Subscription,
    period_start: dt.date | None = None,
    period_end: dt.date | None = None,
    coupon: Coupon | None = None,
    actor: Any = None,
) -> Invoice:
    """Raise the invoice for one period. Idempotent per (subscription, period)."""
    start = period_start or subscription.current_period_start
    end = period_end or subscription.current_period_end

    existing = Invoice.objects.filter(subscription=subscription, period_start=start).first()
    if existing is not None:
        return existing

    plan = subscription.plan
    if plan.is_quoted:
        raise InvalidState(
            f"{plan.name} is priced by agreement. Record the agreed invoice by hand."
        )

    subtotal = plan.price
    discount = discount_for(coupon, subtotal=subtotal, plan_id=plan.pk)
    total = max(subtotal - discount, Decimal("0"))

    # The unique constraint decides the race, not the read above: two
    # concurrent renewals both pass the `existing` check, and exactly one
    # insert survives. The loser reads back the winner's invoice rather
    # than raising, because both callers wanted the same thing.
    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                subscription=subscription,
                number=_placeholder(),
                state=InvoiceState.OPEN if total > 0 else InvoiceState.PAID,
                subtotal=subtotal,
                discount=discount,
                total=total,
                currency=plan.currency,
                period_start=start,
                period_end=end,
                issued_on=timezone.localdate(),
                due_on=timezone.localdate() + dt.timedelta(days=DUE_DAYS),
                paid_at=timezone.now() if total == 0 else None,
                coupon=coupon,
                lines=[
                    {
                        "description": f"{plan.name} — {start.isoformat()} to {end.isoformat()}",
                        "amount": str(subtotal),
                        "currency": plan.currency,
                    }
                ],
            )
    except IntegrityError:
        settled = Invoice.objects.filter(subscription=subscription, period_start=start).first()
        if settled is None:  # pragma: no cover - only reachable on a different clash
            raise
        return settled

    invoice.number = _number_for(invoice)
    invoice.save(update_fields=["number", "updated_at"])

    if coupon is not None and discount > 0:
        # Through `coupons.redeem`, which increments in the database. This used
        # to write `coupon.redemptions + 1` from the object in hand, so two
        # invoices raised with the same coupon counted once, and a capped
        # coupon could be used past its cap. A coupon that did not apply —
        # another plan's, or expired — took nothing off and is not counted.
        from . import coupons as coupon_service

        coupon_service.redeem(coupon=coupon, actor=actor)

    audit.record(
        verb="billing.invoice_issued",
        actor=actor,
        company=subscription.company,
        target=invoice,
        metadata={"total": str(total), "currency": plan.currency},
    )
    events.emit(
        "invoice.issued",
        invoice,
        payload={
            "invoice_number": invoice.number,
            "subscription_public_id": str(subscription.public_id),
            "due_on": invoice.due_on.isoformat(),
        },
    )
    return invoice


@transaction.atomic
def mark_paid(*, invoice: Invoice, provider: str, actor: Any = None) -> Invoice:
    """Settle an invoice. Idempotent: a webhook replay is the normal case."""
    if invoice.state == InvoiceState.PAID:
        return invoice
    if invoice.state in (InvoiceState.VOID, InvoiceState.UNCOLLECTIBLE):
        raise InvalidState("That invoice was written off and cannot be settled.")

    invoice.state = InvoiceState.PAID
    invoice.paid_at = timezone.now()
    invoice.save(update_fields=["state", "paid_at", "updated_at"])

    audit.record(
        verb="billing.invoice_paid",
        actor=actor,
        company=invoice.subscription.company,
        target=invoice,
        metadata={"provider": provider},
    )
    events.emit(
        "invoice.paid",
        invoice,
        payload={
            "invoice_number": invoice.number,
            "subscription_public_id": str(invoice.subscription.public_id),
            "provider": provider,
        },
    )
    return invoice


@transaction.atomic
def void(*, invoice: Invoice, actor: Any = None, reason: str = "") -> Invoice:
    """Cancel an unpaid invoice. A paid one is refunded, never voided."""
    if invoice.state == InvoiceState.PAID:
        raise InvalidState("A paid invoice is refunded, not voided.")

    invoice.state = InvoiceState.VOID
    invoice.save(update_fields=["state", "updated_at"])
    audit.record(
        verb="billing.invoice_voided",
        actor=actor,
        company=invoice.subscription.company,
        target=invoice,
        metadata={"reason": reason},
    )
    return invoice


def discount_for(coupon: Coupon | None, *, subtotal: Decimal, plan_id: int) -> Decimal:
    """What a coupon takes off, or zero.

    Silently zero for a coupon that does not apply, because this runs during
    invoicing where the customer is not present to be told. The refusal that a
    person sees happens in `coupons.validate`, at the moment they type it.
    """
    if coupon is None or not coupon.is_redeemable:
        return Decimal("0")
    if coupon.plan_id is not None and coupon.plan_id != plan_id:
        return Decimal("0")

    if coupon.percent_off is not None:
        return (subtotal * Decimal(coupon.percent_off) / Decimal(100)).quantize(Decimal("0.01"))
    return min(coupon.amount_off or Decimal("0"), subtotal)


def overdue_for(owner: Any, *, today: dt.date | None = None) -> Invoice | None:
    """The oldest unpaid invoice past its due date for one owner, on any of
    their subscriptions, ended ones included (decision D-08).

    An owner holding one may not start a subscription or move to a paid plan:
    otherwise ending an unpaid plan and starting a new one would be a way to
    keep a paid plan without ever paying.
    """
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company

    today = today or timezone.localdate()
    invoices = Invoice.objects.filter(state=InvoiceState.OPEN, due_on__lt=today)
    if isinstance(owner, Company):
        invoices = invoices.filter(subscription__company=owner)
    elif isinstance(owner, CandidateProfile):
        invoices = invoices.filter(subscription__candidate=owner)
    else:
        profile = getattr(owner, "candidate_profile", None)
        if profile is None:
            return None
        invoices = invoices.filter(subscription__candidate=profile)
    return invoices.order_by("due_on", "id").first()


def overdue(*, today: dt.date | None = None) -> list[Invoice]:
    today = today or timezone.localdate()
    return list(
        Invoice.objects.filter(state=InvoiceState.OPEN, due_on__lt=today).select_related(
            "subscription", "subscription__plan"
        )
    )


# --- internals ------------------------------------------------------------


def _placeholder() -> str:
    """A unique value that survives the insert until the real number lands."""
    from uuid import uuid4

    return f"pending-{uuid4().hex[:20]}"


def _number_for(invoice: Invoice) -> str:
    return f"INV-{invoice.issued_on:%Y%m}-{invoice.pk:06d}"
