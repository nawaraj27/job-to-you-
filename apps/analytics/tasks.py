"""
Nightly analytics work (SRS §16.1).

All on the `default` queue: nothing here calls a model, and putting a rollup on
the `ai` queue would have it competing with the work that actually needs a
provider.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task

logger = logging.getLogger(__name__)

#: How long raw events are kept (§5.15: "partitioned and pruned"). A year, which
#: covers a full hiring cycle for the year-on-year comparisons §16.2 implies,
#: and past which the rollups are the record.
EVENT_RETENTION_DAYS = 365


@shared_task(queue="default")
def run_rollups(day: str = "") -> dict[str, int]:
    """Roll up one day. Idempotent, so a retry is safe."""
    from .services import rollups

    parsed = dt.date.fromisoformat(day) if day else None
    return rollups.run_all(day=parsed)


@shared_task(queue="default")
def measure_steering(day: str = "") -> int:
    """Take the four measurements (§16.3)."""
    from .services import steering

    parsed = dt.date.fromisoformat(day) if day else None
    return len(steering.measure_all(day=parsed))


@shared_task(queue="default")
def prune_events(days: int = EVENT_RETENTION_DAYS, limit: int = 50_000) -> int:
    """Delete raw events past their retention.

    Bounded per run: an unbounded delete over a partition-sized table takes a
    lock nobody planned for. Running it nightly against a bounded batch means
    the table drains steadily rather than in one alarming maintenance window.
    """
    from django.utils import timezone

    from .models import AnalyticsEvent

    cutoff = timezone.now() - dt.timedelta(days=days)
    stale = AnalyticsEvent.objects.filter(created_at__lt=cutoff).values_list("pk", flat=True)[
        :limit
    ]
    deleted, _ = AnalyticsEvent.objects.filter(pk__in=list(stale)).delete()

    if deleted:
        logger.info("pruned %s analytics event(s)", deleted)
    return deleted


@shared_task(queue="default")
def backfill_rollups(days: int = 30) -> dict[str, int]:
    """Recompute a range of days.

    Exists because a rollup bug is always found after it has been writing wrong
    numbers for a fortnight, and the fix is worthless without a way to correct
    what it already wrote.
    """
    from .services import rollups

    return rollups.backfill(days=days)
