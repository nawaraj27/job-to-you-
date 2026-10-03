"""
Two-stage fan-out (ADR-A25, SRS §13.4).

Stage one plans: the outbox worker resolves the audience **as a query**, takes
its key range, and writes one `FanoutJob` per chunk. Stage two executes: each
chunk re-runs the query over its own range and delivers.

Why not one pass. A `job.published` in a popular category matches fifty
thousand candidates. Single-pass delivery is one transaction writing a hundred
thousand rows, holding locks, blocking the outbox behind it, and starting from
nothing on any failure. Chunks are small, independently retryable, and the
delivery ledger's unique constraint makes a retry physically unable to
double-send.

The cursor is a primary-key range, not an offset. An offset shifts under
concurrent inserts, and the symptom is a recipient silently skipped — which
nothing downstream can detect.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import Max, Min
from django.utils import timezone

from ..events import spec_for
from ..models import (
    Channel,
    DeliveryStatus,
    FanoutJob,
    FanoutState,
    NotificationDelivery,
    OutboxEvent,
)
from . import audiences

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

    from ..events import EventSpec

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500

#: A chunk that keeps failing is parked rather than retried forever. Five
#: attempts with backoff spans about half an hour, which is longer than any
#: transient failure this system has.
MAX_ATTEMPTS = 5


def plan(event: OutboxEvent) -> int:
    """Stage one. Returns the number of chunks written.

    Idempotent: an event whose chunks already exist is not re-planned, so an
    outbox retry after a partial failure does not double the work.
    """
    spec = spec_for(event.type)
    if not spec.audience:
        return 0
    if FanoutJob.objects.filter(event=event).exists():
        return 0

    recipients = audiences.resolve(spec.audience, event)
    bounds = recipients.aggregate(low=Min("pk"), high=Max("pk"))
    if bounds["low"] is None:
        return 0

    jobs = []
    cursor = bounds["low"]
    # Walk the key space in fixed strides. Chunks may be uneven — key ranges
    # are sparse — and that is fine: the point is a bounded worst case, not
    # equal sizes.
    while cursor <= bounds["high"]:
        end = cursor + CHUNK_SIZE - 1
        jobs.append(
            FanoutJob(
                event=event,
                audience=spec.audience,
                cursor_start=cursor,
                cursor_end=min(end, bounds["high"]),
                chunk_size=CHUNK_SIZE,
            )
        )
        cursor = end + 1

    FanoutJob.objects.bulk_create(jobs)
    return len(jobs)


def run_due(*, limit: int = 50) -> dict[str, int]:
    """Stage two. Claim and execute chunks."""
    ran = delivered = failed = 0

    for job in _claim(limit):
        ran += 1
        try:
            delivered += _execute(job)
        except Exception as exc:  # noqa: BLE001 - recorded, never escapes the worker
            failed += 1
            _defer(job, str(exc))
        else:
            job.state = FanoutState.DONE
            job.completed_at = timezone.now()
            job.save(update_fields=["state", "completed_at", "delivered"])

    return {"chunks": ran, "delivered": delivered, "failed": failed}


def _claim(limit: int) -> list[FanoutJob]:
    """Take chunks off the queue, one worker at a time.

    `FOR UPDATE SKIP LOCKED`, the same mechanism the outbox uses: two workers
    running concurrently take different chunks rather than the same one twice.
    """
    with transaction.atomic():
        jobs = list(
            FanoutJob.objects.claimable()
            .select_for_update(skip_locked=True)
            .select_related("event")[:limit]
        )
        if jobs:
            FanoutJob.objects.filter(pk__in=[job.pk for job in jobs]).update(
                state=FanoutState.RUNNING
            )
    return jobs


def _execute(job: FanoutJob) -> int:
    """Deliver one chunk. Returns how many recipients were newly served."""
    from . import channels

    event = job.event
    spec = spec_for(event.type)
    recipients = audiences.resolve(job.audience, event).filter(
        pk__gte=job.cursor_start, pk__lte=job.cursor_end
    )

    served = 0
    for recipient in recipients.iterator(chunk_size=200):
        served += int(_deliver_to(event, spec, recipient, channels))

    job.delivered = served
    return served


def _deliver_to(event: OutboxEvent, spec: EventSpec, recipient: User, channels: ModuleType) -> bool:
    """One recipient, every channel their preferences allow.

    The ledger row is claimed **first**, in its own transaction: it is what
    makes a retried chunk unable to double-send, and claiming it after the
    send would leave a window where a crash sends twice.
    """
    from . import preferences

    served = False
    for channel in (Channel.IN_APP, Channel.EMAIL):
        allowed = not spec.preference_group or preferences.allows(
            user=recipient, event_key=spec.preference_group, channel=channel
        )
        claim = _claim_delivery(event, recipient, channel)
        if claim is None:
            continue  # already served on an earlier attempt

        if not allowed:
            # Recorded, not skipped silently: "why did I not get that" is a
            # real support question and this table is the only answer to it.
            _settle(claim, DeliveryStatus.MUTED, "preference")
            continue

        try:
            if channel == Channel.IN_APP:
                channels.deliver_in_app(event=event, spec=spec, recipient=recipient)
                _settle(claim, DeliveryStatus.SENT, "")
                served = True
            else:
                status, reason = channels.deliver_email(event=event, spec=spec, recipient=recipient)
                _settle(claim, status, reason)
                served = served or status == DeliveryStatus.SENT
        except Exception as exc:  # noqa: BLE001 - one channel must not sink the rest
            logger.warning(
                "delivery failed",
                extra={"event": event.type, "channel": channel, "error": str(exc)},
            )
            _settle(claim, DeliveryStatus.FAILED, str(exc)[:120])

    return served


def _claim_delivery(
    event: OutboxEvent, recipient: User, channel: str
) -> NotificationDelivery | None:
    """Insert the ledger row, or return None if somebody already has.

    The unique constraint is the guarantee. A `filter().exists()` check first
    would be a race, and the race is what sends a person the same email twice.
    """
    try:
        with transaction.atomic():
            return NotificationDelivery.objects.create(
                event=event,
                recipient=recipient,
                channel=channel,
                status=DeliveryStatus.PENDING,
            )
    except IntegrityError:
        return None


def _settle(delivery: NotificationDelivery, status: str, reason: str) -> None:
    delivery.status = status
    delivery.reason = reason[:120]
    delivery.save(update_fields=["status", "reason"])


def _defer(job: FanoutJob, error: str) -> None:
    """Back off, or park the chunk when it has failed too often."""
    import datetime as dt

    job.attempts += 1
    job.last_error = error[:500]
    job.state = FanoutState.FAILED
    if job.attempts >= MAX_ATTEMPTS:
        job.available_at = timezone.now() + dt.timedelta(days=365)
        logger.error("fanout chunk parked after %s attempts", job.attempts, extra={"chunk": job.pk})
    else:
        job.available_at = timezone.now() + dt.timedelta(seconds=30 * (2**job.attempts))
    job.save(update_fields=["attempts", "last_error", "state", "available_at"])
