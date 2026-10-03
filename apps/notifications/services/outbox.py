"""
Draining the outbox (SRS §13.4).

One batch at a time, with `FOR UPDATE SKIP LOCKED` so several workers can drain
concurrently without any of them waiting on another's rows.

Per event: run each subscriber exactly once, recorded in the ledger. If every
subscriber succeeds, the event is done. If any fails, the event is retried with
exponential backoff — and because the ledger already records the subscribers
that succeeded, a retry only re-runs the ones that did not.

After `OUTBOX_MAX_ATTEMPTS` the event is dead-lettered rather than retried
forever. Dead-lettered events stay visible; they are an alert, not a bin.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.logging import bind, reset

from ..consumers import consumers_for, name_of
from ..models import OutboxConsumerRun, OutboxEvent

logger = logging.getLogger(__name__)

#: Backoff in seconds by attempt number: ~2s, 8s, 30s, 2m, 8m, then 30m.
BACKOFF_BASE_SECONDS = 2
BACKOFF_CAP_SECONDS = 30 * 60


def drain(limit: int | None = None) -> int:
    """Process one batch. Returns the number of events completed."""
    batch_size = limit or settings.OUTBOX_BATCH_SIZE
    completed = 0

    with transaction.atomic():
        events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .pending()
            .order_by("available_at", "id")[:batch_size]
        )

        for event in events:
            if _dispatch(event):
                completed += 1

    return completed


def _dispatch(event: OutboxEvent) -> bool:
    """Run every subscriber for one event. True when the event is finished."""
    failures: list[str] = []
    context_token = bind(event_id=event.pk)
    try:
        return _dispatch_inner(event, failures)
    finally:
        reset(context_token)


def _dispatch_inner(event: OutboxEvent, failures: list[str]) -> bool:

    for consumer in consumers_for(event.type):
        consumer_name = name_of(consumer)

        if _already_ran(event, consumer_name):
            continue

        try:
            # A savepoint per consumer. Without it, a database error raised by
            # one consumer would poison the batch's transaction and every
            # subsequent query in it would fail too.
            with transaction.atomic():
                consumer(event)
        except Exception as exc:  # noqa: BLE001 - one consumer must not stop the rest
            failures.append(f"{consumer_name}: {exc}")
            logger.exception(
                "event consumer failed",
                extra={"event_type": event.type, "event_id": event.pk, "consumer": consumer_name},
            )
            _record_run(event, consumer_name, OutboxConsumerRun.Status.FAILED, str(exc))
        else:
            _record_run(event, consumer_name, OutboxConsumerRun.Status.OK, "")

    now = timezone.now()
    event.attempts += 1

    if not failures:
        event.processed_at = now
        event.last_error = ""
        event.save(update_fields=["processed_at", "attempts", "last_error"])
        return True

    event.last_error = " | ".join(failures)[:2000]

    if event.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
        event.dead_lettered_at = now
        logger.error(
            "event dead-lettered",
            extra={"event_type": event.type, "event_id": event.pk, "attempts": event.attempts},
        )
        event.save(update_fields=["dead_lettered_at", "attempts", "last_error"])
    else:
        event.available_at = now + timedelta(seconds=_backoff(event.attempts))
        event.save(update_fields=["available_at", "attempts", "last_error"])

    return False


def _already_ran(event: OutboxEvent, consumer_name: str) -> bool:
    """True when this consumer has already succeeded for this event."""
    return OutboxConsumerRun.objects.filter(
        event=event, consumer=consumer_name, status=OutboxConsumerRun.Status.OK
    ).exists()


def _record_run(event: OutboxEvent, consumer_name: str, status: str, error: str) -> None:
    """Write the ledger row, tolerating a concurrent writer.

    The unique constraint is the guarantee; this only records the outcome. A
    failed run is overwritten on the next attempt so the ledger reflects the
    latest verdict rather than the first.
    """
    try:
        with transaction.atomic():
            OutboxConsumerRun.objects.create(
                event=event, consumer=consumer_name, status=status, error=error[:2000]
            )
    except IntegrityError:
        OutboxConsumerRun.objects.filter(event=event, consumer=consumer_name).update(
            status=status, error=error[:2000]
        )


def _backoff(attempt: int) -> int:
    return min(BACKOFF_BASE_SECONDS * (4 ** (attempt - 1)), BACKOFF_CAP_SECONDS)


def purge_processed(*, older_than_hours: int | None = None) -> int:
    """Delete processed events past the retention window.

    The outbox is a queue. Keeping delivered events would make it the largest
    table in the database within a year, and it is not the history — AuditLog
    and the domain timelines are (SRS §5.15).
    """
    hours = older_than_hours if older_than_hours is not None else settings.OUTBOX_RETENTION_HOURS
    cutoff = timezone.now() - timedelta(hours=hours)
    # QuerySet.delete() returns the total across every cascaded model, which
    # here includes the ledger rows. Only the event count is meaningful.
    _, per_model = OutboxEvent.objects.filter(processed_at__lte=cutoff).delete()
    deleted = per_model.get(OutboxEvent._meta.label, 0)
    if deleted:
        logger.info("purged processed outbox events", extra={"count": deleted})
    return deleted
