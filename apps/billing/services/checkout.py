"""
Paying an invoice through a hosted gateway (ADR-A58).

The sequence, and who is believed at each step:

  1. `begin` — a `PaymentAttempt` is written **before** the customer leaves:
     the invoice, the amount and a random reference. This row is the only thing
     a later callback is checked against.
  2. The customer pays on the gateway's page (or does not).
  3. `handle_return` — the browser comes back. The gateway module checks what
     it can locally (eSewa's signature, Khalti's pidx) and the attempt is found
     by its reference. A return naming an unknown reference, a different
     amount or a forged signature is refused and **writes nothing**.
  4. `reconcile` — the gateway's status service is asked, server to server.
     Its answer, and only its answer, moves the attempt. A success is recorded
     through `payments._record`, the same path a bank transfer takes, so the
     invoice is marked paid and a past-due subscription reactivated in the same
     transaction.

**Idempotent by construction.** The attempt row is locked while its outcome is
written, and a settled attempt is answered from the row. A replayed or
duplicated callback, two tabs returning at once and the background re-check all
land on the same `Payment` — and `Payment` is unique on (provider,
provider_ref) underneath that, with the attempt's own reference as the ref.

**An unreachable gateway is not a failed payment.** The customer may have paid.
The attempt stays open, says so, and `reconcile_open` asks again.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState

from ..gateways import base as gateways
from ..models import (
    AttemptState,
    Invoice,
    InvoiceState,
    Payment,
    PaymentAttempt,
    PaymentState,
)
from ..providers.base import PaymentUnverified, Settlement
from . import payments

log = logging.getLogger("careerai.billing.checkout")

#: An attempt the gateway has never heard of is abandoned after this long.
#: eSewa fails a payment not made within 5 minutes of login; Khalti's link
#: lives 30 minutes. The longer of the two, with room for a slow customer.
ABANDON_AFTER = dt.timedelta(minutes=45)
#: How long the background re-check keeps asking about an open attempt.
RECHECK_FOR = dt.timedelta(days=3)


def offered(invoice: Invoice) -> list[gateways.Gateway]:
    """Gateways that can take this invoice: enabled, configured, its currency."""
    if invoice.state != InvoiceState.OPEN or invoice.total <= 0:
        return []
    return [g for g in gateways.enabled() if invoice.currency.upper() in g.currencies]


def awaiting(invoice: Invoice) -> PaymentAttempt | None:
    """An attempt the gateway is still deciding. A second payment waits for it."""
    return (
        PaymentAttempt.objects.filter(invoice=invoice, state=AttemptState.PENDING)
        .order_by("-created_at")
        .first()
    )


def begin(
    *,
    invoice: Invoice,
    gateway_key: str,
    actor: Any,
    urls_for: Callable[[str], gateways.ReturnUrls],
) -> tuple[PaymentAttempt, gateways.Checkout]:
    """Write the attempt, then ask the gateway where to send the customer."""
    gateway = gateways.get(gateway_key)
    if gateways.environment() != "test":
        raise gateways.GatewayNotConfigured("Online payment is only available in test mode.")
    if gateway not in offered(invoice):
        if invoice.state == InvoiceState.PAID:
            raise InvalidState("That invoice is already paid.")
        if invoice.state != InvoiceState.OPEN:
            raise InvalidState("That invoice is closed.")
        raise gateways.GatewayNotConfigured(f"{gateway.label} is not available for this invoice.")

    pending = awaiting(invoice)
    if pending is not None:
        pending = reconcile(pending)
        invoice.refresh_from_db()
        if invoice.state == InvoiceState.PAID:
            raise InvalidState("That invoice is already paid.")
        if pending.state == AttemptState.PENDING:
            raise InvalidState(
                f"A {pending.provider_label} payment for this invoice is still being "
                "confirmed. Check its status before paying again."
            )

    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.state != InvoiceState.OPEN:
            raise InvalidState(
                "That invoice is already paid."
                if locked.state == InvoiceState.PAID
                else "That invoice is closed."
            )
        attempt = PaymentAttempt.objects.create(
            invoice=locked,
            provider=gateway.key,
            environment=gateways.environment(),
            # Random, so a reference cannot be guessed and its return replayed
            # against somebody else's attempt. eSewa allows [A-Za-z0-9-].
            reference=f"ca-{uuid.uuid4().hex}",
            amount=locked.total,
            currency=locked.currency,
            initiated_by=actor if getattr(actor, "pk", None) else None,
        )

    try:
        checkout = gateway.begin(attempt=attempt, urls=urls_for(attempt.reference))
    except (gateways.GatewayUnavailable, PaymentUnverified) as error:
        attempt.state = AttemptState.FAILED
        attempt.failure_reason = str(error.message)[:300]
        attempt.completed_at = timezone.now()
        attempt.save(update_fields=["state", "failure_reason", "completed_at", "updated_at"])
        raise

    attempt.gateway_token = checkout.gateway_token
    attempt.detail = {"begin": checkout.detail} if checkout.detail else {}
    attempt.save(update_fields=["gateway_token", "detail", "updated_at"])
    audit.record(
        verb="billing.payment_started",
        actor=actor,
        company=invoice.subscription.company,
        target=attempt,
        metadata={
            "provider": gateway.key,
            "environment": attempt.environment,
            "invoice": invoice.number,
            "amount": str(attempt.amount),
        },
    )
    return attempt, checkout


def handle_return(
    *, gateway_key: str, reference: str, params: dict[str, str], failed: bool
) -> PaymentAttempt:
    """The customer's browser came back. Check it, then ask the gateway."""
    gateway = gateways.get(gateway_key)
    attempt = PaymentAttempt.objects.filter(provider=gateway.key, reference=reference).first()
    if attempt is None:
        log.warning("payment return for unknown reference", extra={"provider": gateway_key})
        raise PaymentUnverified("That payment is not one this site started.")

    trip = gateway.parse_return(reference=reference, params=params, failed=failed)
    if attempt.gateway_token and trip.gateway_token and trip.gateway_token != attempt.gateway_token:
        raise PaymentUnverified("That payment response is for a different payment.")
    if trip.signed_amount is not None and trip.signed_amount != attempt.amount:
        log.warning("payment return amount mismatch", extra={"attempt": str(attempt.public_id)})
        raise PaymentUnverified("That payment response is for a different amount.")

    return reconcile(attempt, browser_failed=failed, trip=trip)


def reconcile(
    attempt: PaymentAttempt,
    *,
    browser_failed: bool = False,
    trip: gateways.ReturnTrip | None = None,
) -> PaymentAttempt:
    """Ask the gateway what happened to this attempt, and record its answer."""
    if attempt.state in (AttemptState.SUCCEEDED, AttemptState.REFUNDED) and trip is not None:
        # A replayed or duplicate return for a settled attempt: answered from
        # the row, without asking the gateway or writing anything.
        return attempt

    gateway = gateways.get(attempt.provider)
    try:
        lookup = gateway.lookup(attempt=attempt)
    except gateways.GatewayUnavailable as error:
        return _unreachable(attempt, reason=str(error.message), came_back=trip is not None)

    with transaction.atomic():
        locked = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
        _apply(locked, lookup, browser_failed=browser_failed, trip=trip)
    return locked


def reconcile_open(*, now: dt.datetime | None = None, limit: int = 100) -> int:
    """The background re-check. Open attempts are asked about until they settle,
    are abandoned, or are too old to be worth asking about."""
    now = now or timezone.now()
    due = PaymentAttempt.objects.filter(
        state__in=(AttemptState.INITIATED, AttemptState.PENDING),
        created_at__gte=now - RECHECK_FOR,
        created_at__lte=now - dt.timedelta(minutes=2),
    ).order_by("last_checked_at", "created_at")[:limit]
    checked = 0
    for attempt in due:
        try:
            reconcile(attempt)
        except gateways.GatewayNotConfigured:
            continue
        checked += 1
    return checked


# --- internals ---------------------------------------------------------------


def _unreachable(attempt: PaymentAttempt, *, reason: str, came_back: bool) -> PaymentAttempt:
    with transaction.atomic():
        locked = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.is_final:
            return locked
        if came_back and locked.state == AttemptState.INITIATED:
            # The customer came back from the gateway, so it may hold money.
            locked.state = AttemptState.PENDING
        locked.failure_reason = reason[:300]
        locked.checks += 1
        locked.last_checked_at = timezone.now()
        locked.save(
            update_fields=["state", "failure_reason", "checks", "last_checked_at", "updated_at"]
        )
    return locked


def _apply(
    attempt: PaymentAttempt,
    lookup: gateways.Lookup,
    *,
    browser_failed: bool,
    trip: gateways.ReturnTrip | None,
) -> None:
    now = timezone.now()
    attempt.checks += 1
    attempt.last_checked_at = now
    attempt.gateway_status = lookup.gateway_status[:40]
    if lookup.gateway_ref:
        attempt.gateway_ref = lookup.gateway_ref[:120]
    detail = dict(attempt.detail or {})
    detail["last_lookup"] = lookup.detail
    if trip is not None and trip.detail:
        detail["last_return"] = trip.detail
    attempt.detail = detail
    fields = ["checks", "last_checked_at", "gateway_status", "gateway_ref", "detail", "updated_at"]

    previous = attempt.state
    target = lookup.state

    if previous == AttemptState.SUCCEEDED and target == AttemptState.SUCCEEDED:
        attempt.save(update_fields=fields)
        return

    if target == AttemptState.SUCCEEDED:
        if lookup.amount is not None and lookup.amount != attempt.amount:
            # Money moved, but not the amount this site asked for. Neither paid
            # nor failed: a person has to look at it.
            attempt.state = AttemptState.PENDING
            attempt.failure_reason = (
                f"The gateway reports {lookup.amount}, not the {attempt.amount} requested. "
                "Held for staff review."
            )
            attempt.save(update_fields=[*fields, "state", "failure_reason"])
            _audit(attempt, "billing.payment_amount_mismatch", {"reported": str(lookup.amount)})
            return
        _settle(attempt)
        attempt.state = AttemptState.SUCCEEDED
        attempt.completed_at = now
        attempt.save(update_fields=[*fields, "state", "failure_reason", "completed_at", "payment"])
        _audit(attempt, "billing.payment_confirmed", {"gateway_ref": attempt.gateway_ref})
        return

    if target == AttemptState.REFUNDED:
        attempt.state = AttemptState.REFUNDED
        attempt.completed_at = attempt.completed_at or now
        attempt.failure_reason = f"Refunded at the gateway ({lookup.gateway_status})."
        if attempt.payment_id:
            Payment.objects.filter(pk=attempt.payment_id).update(
                state=PaymentState.REFUNDED, updated_at=now
            )
        attempt.save(update_fields=[*fields, "state", "completed_at", "failure_reason"])
        _audit(attempt, "billing.payment_refunded", {"status": lookup.gateway_status})
        return

    if previous == AttemptState.SUCCEEDED:
        # Only a refund may move a settled attempt. Anything else is the
        # gateway contradicting itself, which is recorded but not acted on.
        attempt.save(update_fields=fields)
        log.warning(
            "gateway contradicted a settled attempt", extra={"attempt": str(attempt.public_id)}
        )
        return

    if target == AttemptState.INITIATED:
        # The gateway has no payment under this reference.
        age = now - attempt.created_at
        if browser_failed:
            attempt.state = AttemptState.CANCELLED
            attempt.failure_reason = f"The payment was not completed at {attempt.provider_label}."
        elif age > ABANDON_AFTER:
            attempt.state = AttemptState.EXPIRED
            attempt.failure_reason = "No payment arrived before the payment window closed."
        elif trip is not None and attempt.state == AttemptState.INITIATED:
            # Came back saying "paid" but the gateway has nothing yet: wait.
            attempt.state = AttemptState.PENDING
    else:
        attempt.state = target
        if target == AttemptState.CANCELLED:
            attempt.failure_reason = "The payment was cancelled."
        elif target == AttemptState.EXPIRED:
            attempt.failure_reason = "The payment link expired before it was used."
        elif target == AttemptState.FAILED:
            attempt.failure_reason = "The gateway reported the payment as failed."
        elif target == AttemptState.PENDING:
            attempt.failure_reason = ""

    if attempt.is_final:
        attempt.completed_at = now
    attempt.save(update_fields=[*fields, "state", "failure_reason", "completed_at"])
    if attempt.state != previous:
        _audit(attempt, f"billing.payment_{attempt.state}", {"status": lookup.gateway_status})


def _settle(attempt: PaymentAttempt) -> None:
    """Record the money. Called with the attempt row locked."""
    invoice = Invoice.objects.select_for_update().get(pk=attempt.invoice_id)
    settlement = Settlement(
        # The attempt's own reference: one attempt can produce one payment,
        # whatever the gateway calls it, however many times it is reported.
        reference=attempt.reference,
        amount=attempt.amount,
        currency=attempt.currency,
        succeeded=True,
        detail={
            "gateway_ref": attempt.gateway_ref,
            "environment": attempt.environment,
            "attempt": str(attempt.public_id),
        },
    )
    if invoice.state == InvoiceState.OPEN:
        attempt.payment = payments._record(
            invoice=invoice, provider_key=attempt.provider, settlement=settlement, actor=None
        )
        attempt.failure_reason = ""
        return

    # Paid twice (two tabs, two gateways) or paid after being voided. The money
    # arrived and is recorded as such; the invoice is not touched, and the row
    # says a refund is owed so support sees it without being told.
    payment, _ = Payment.objects.get_or_create(
        provider=attempt.provider,
        provider_ref=attempt.reference,
        defaults={
            "invoice": invoice,
            "state": PaymentState.SUCCEEDED,
            "amount": attempt.amount,
            "currency": attempt.currency,
            "detail": {**settlement.detail, "refund_owed": True},
            "received_at": timezone.now(),
        },
    )
    attempt.payment = payment
    attempt.failure_reason = (
        f"Invoice {invoice.number} was already {invoice.get_state_display().lower()} when this "
        "payment arrived. A refund is owed."
    )
    _audit(attempt, "billing.payment_refund_owed", {"invoice_state": invoice.state})


def _audit(attempt: PaymentAttempt, verb: str, metadata: dict[str, Any]) -> None:
    audit.record(
        verb=verb,
        actor=None,
        company=attempt.invoice.subscription.company,
        target=attempt,
        metadata={
            "provider": attempt.provider,
            "environment": attempt.environment,
            "reference": attempt.reference,
            "amount": str(attempt.amount),
            **metadata,
        },
    )


def annotate(invoices: list[Invoice]) -> list[Invoice]:
    """Attach what an invoice row needs to offer payment: the gateways that can
    take it and any payment of it still being confirmed."""
    for invoice in invoices:
        invoice.pay_with = offered(invoice)  # type: ignore[attr-defined]
        invoice.open_attempt = awaiting(invoice) if invoice.state == InvoiceState.OPEN else None  # type: ignore[attr-defined]
    return invoices
