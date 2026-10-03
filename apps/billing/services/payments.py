"""
Taking money and recording that it arrived (SRS §5.14, ADR-A15).

**A settlement is idempotent on (provider, provider_ref).** Every payment
gateway retries its callback, and several send the same one twice on principle.
The unique constraint on `Payment` is what makes the second delivery a no-op
instead of a second payment, and it is a database constraint rather than a
check in this module because the two callbacks can arrive at the same moment.

**The amount is the provider's, never the caller's.** `settle` reads the
verified `Settlement` and compares it against the invoice; a callback claiming
to have paid 1 NPR for a 1,200 NPR invoice leaves the invoice open and records
the underpayment, rather than marking it paid because a webhook said so.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState

from ..models import Invoice, InvoiceState, Payment, PaymentState
from ..providers import base as providers
from . import invoices as invoice_service


def start(*, invoice: Invoice, provider_key: str) -> providers.Charge:
    """Begin a payment. Returns where to send the customer."""
    if invoice.state == InvoiceState.PAID:
        raise InvalidState("That invoice is already paid.")
    if invoice.state in (InvoiceState.VOID, InvoiceState.UNCOLLECTIBLE):
        raise InvalidState("That invoice is closed.")

    return providers.get(provider_key).start(invoice=invoice)


@transaction.atomic
def settle_callback(*, provider_key: str, payload: dict[str, Any]) -> Payment:
    """Handle a provider's callback: verify, then record.

    Verification happens before anything is written. A payload that cannot be
    verified is not a failed payment — it is not a payment at all — and writing
    a row for it would let anybody who can reach the endpoint fill the table.
    """
    provider = providers.get(provider_key)
    settlement = provider.verify(payload=payload)

    invoice = Invoice.objects.select_for_update().filter(number=payload.get("invoice", "")).first()
    if invoice is None:
        raise InvalidState("That payment does not name an invoice this platform issued.")

    return _record(
        invoice=invoice,
        provider_key=provider_key,
        settlement=settlement,
        actor=None,
    )


@transaction.atomic
def record_manual(
    *, invoice: Invoice, actor: Any, reference: str, amount: Decimal | None = None
) -> Payment:
    """A person recording a bank transfer they have seen on a statement."""
    from ..providers.manual import ManualProvider

    if not reference.strip():
        raise InvalidState("Record the bank reference, so the payment can be traced.")

    settlement = ManualProvider().settle(
        invoice=invoice, reference=reference.strip(), amount=amount or invoice.total
    )
    return _record(invoice=invoice, provider_key="manual", settlement=settlement, actor=actor)


def _record(
    *,
    invoice: Invoice,
    provider_key: str,
    settlement: providers.Settlement,
    actor: Any,
) -> Payment:
    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                invoice=invoice,
                provider=provider_key,
                provider_ref=settlement.reference,
                state=PaymentState.SUCCEEDED if settlement.succeeded else PaymentState.FAILED,
                amount=settlement.amount,
                currency=settlement.currency,
                detail=settlement.detail,
                received_at=timezone.now(),
                recorded_by=actor,
            )
    except IntegrityError:
        # The gateway sent the callback twice, which is normal. Return what the
        # first delivery created rather than taking the money again.
        existing = Payment.objects.filter(
            provider=provider_key, provider_ref=settlement.reference
        ).first()
        if existing is None:  # pragma: no cover - only reachable on a different clash
            raise Conflict("That payment could not be recorded.") from None
        return existing

    audit.record(
        verb="billing.payment_recorded",
        actor=actor,
        company=invoice.subscription.company,
        target=payment,
        metadata={
            "provider": provider_key,
            "amount": str(settlement.amount),
            "succeeded": settlement.succeeded,
        },
    )

    # The currency is part of the amount. "12.00" in NPR is not "12.00" in USD,
    # and comparing the numbers alone settled a USD invoice with a hundredth of
    # what was owed.
    same_currency = settlement.currency.upper() == invoice.currency.upper()
    if settlement.succeeded and same_currency and settlement.amount >= invoice.total:
        # A checkout's plan starts with its payment (ADR-A59), in this
        # transaction, before the invoice is marked paid.
        from .subscriptions import activate_paid

        activate_paid(invoice=invoice)
        invoice_service.mark_paid(invoice=invoice, provider=provider_key, actor=actor)
        _reactivate(invoice)

    return payment


def _reactivate(invoice: Invoice) -> None:
    """A `past_due` subscription whose unpaid invoices are all settled goes back to active.

    Only when *nothing* else is overdue: paying this month's invoice while last
    month's is still unpaid used to clear `past_due` and restart the grace
    period on a debt that remained.
    """
    from django.utils import timezone

    from ..models import SubscriptionState

    subscription = invoice.subscription
    if subscription.state != SubscriptionState.PAST_DUE:
        return
    still_overdue = (
        Invoice.objects.filter(
            subscription=subscription,
            state=InvoiceState.OPEN,
            due_on__lt=timezone.localdate(),
        )
        .exclude(pk=invoice.pk)
        .exists()
    )
    if still_overdue:
        return
    subscription.state = SubscriptionState.ACTIVE
    subscription.save(update_fields=["state", "updated_at"])
