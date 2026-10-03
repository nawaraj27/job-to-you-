"""
Nightly billing work (SRS §19: the subscription state machine's cron trigger).

Four tasks, in the order they must run: renew what is due, chase what is
unpaid, end what was cancelled, and end what was never paid. Running them in
one task would mean a failure halfway through leaves the system in a state
nobody named; running them in this order means a subscription cancelled *and*
overdue is ended once, as a cancellation, which is the truthful reason.

All on the `default` queue. Only `reconcile_payments` calls a provider (a
payment gateway's status service); nothing here calls a model.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="default")
def renew_due(day: str = "") -> int:
    """Roll periods forward and raise the next invoice.

    Idempotent twice over: `renew` moves a period that has actually ended, and
    `issue` is keyed on (subscription, period), so a double run bills nobody
    twice.
    """
    from django.db import transaction
    from django.utils import timezone

    from .models import LIVE_STATES, Subscription, SubscriptionState
    from .services import invoices, subscriptions

    today = dt.date.fromisoformat(day) if day else timezone.localdate()
    # Decision D-08: a move to Free scheduled on a `past_due` subscription
    # still happens on its date. Renewable subscriptions apply theirs in `renew`.
    subscriptions.apply_due_downgrades(today=today)
    # A `past_due` subscription is not renewed. Renewing it moved
    # `current_period_end` forward every month, and `end_unpaid` measures its
    # grace from that date — so an account that never paid kept its plan for
    # ever while unpaid invoices piled up (SRS §5.14: withdrawal after fourteen
    # days). Paying reactivates it, and the next night renews it.
    due = (
        Subscription.objects.filter(
            state__in=LIVE_STATES, current_period_end__lte=today, cancel_at__isnull=True
        )
        .exclude(state=SubscriptionState.PAST_DUE)
        .select_related("plan")
    )

    renewed = 0
    for candidate in due:
        # Locked and re-read. Two overlapping runs (Celery delivers a scheduled
        # task at least once) both read the subscription as due and both
        # renewed it: the period and the invoice came out right by luck of
        # identical arithmetic, but `subscription.renewed` was emitted twice.
        with transaction.atomic():
            subscription = due.select_for_update(of=("self",)).filter(pk=candidate.pk).first()
            if subscription is None:
                continue  # the other run renewed it while this one waited
            subscriptions.renew(subscription=subscription)
            if not subscription.plan.is_free and not subscription.plan.is_quoted:
                invoices.issue(subscription=subscription)
        renewed += 1

    if renewed:
        logger.info("renewed %s subscription(s)", renewed)
    return renewed


@shared_task(queue="default")
def chase_overdue(day: str = "") -> int:
    """Move subscriptions with an overdue invoice to `past_due`.

    The plan keeps working. This is the notification, not the punishment.
    """
    from django.utils import timezone

    from .services import invoices, subscriptions

    today = dt.date.fromisoformat(day) if day else timezone.localdate()
    chased = 0
    for invoice in invoices.overdue(today=today):
        subscriptions.mark_past_due(
            subscription=invoice.subscription, invoice_number=invoice.number
        )
        chased += 1
    return chased


@shared_task(queue="default")
def end_cancelled(day: str = "") -> int:
    """Withdraw entitlements from subscriptions whose end date has arrived."""
    from django.utils import timezone

    from .services import subscriptions

    today = dt.date.fromisoformat(day) if day else timezone.localdate()
    return subscriptions.expire_due(today=today)


@shared_task(queue="default")
def end_unpaid(day: str = "") -> int:
    """Withdraw entitlements after the grace period on an unpaid invoice."""
    from django.utils import timezone

    from .services import subscriptions

    today = dt.date.fromisoformat(day) if day else timezone.localdate()
    return subscriptions.expire_overdue(today=today)


@shared_task(queue="default")
def prune_usage(months: int = 13) -> int:
    """Delete usage counters older than a year and a change.

    Thirteen months rather than twelve: "what did I use this month last year"
    is a question somebody asks in the first week of the month, and a retention
    of exactly a year answers it with nothing.
    """
    from django.utils import timezone

    from .services import usage

    today = timezone.localdate()
    year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
    return usage.prune(before=dt.date(year, month + 1, 1))


@shared_task(queue="default")
def reconcile_payments() -> int:
    """Ask the gateways about payments still open (ADR-A58).

    The one task here that calls a provider: a customer who paid and closed
    the tab before coming back has paid, and only the gateway can say so.
    """
    from .services import checkout

    checked = checkout.reconcile_open()
    if checked:
        logger.info("reconciled %s open gateway payment(s)", checked)
    return checked


@shared_task(queue="default")
def expire_checkouts() -> int:
    """Close unpaid checkouts older than CHECKOUT_EXPIRES_DAYS (ADR-A59)."""
    from .services import subscriptions

    closed = subscriptions.expire_unpaid_checkouts()
    if closed:
        logger.info("closed %s unpaid checkout(s)", closed)
    return closed
