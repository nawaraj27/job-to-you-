"""Outbox background work."""

from __future__ import annotations

from celery import shared_task

from .services import outbox


@shared_task(queue="default")
def drain_outbox(limit: int | None = None) -> int:
    """Process one batch.

    A dedicated long-running worker (`manage.py outbox_worker`) is the primary
    drain; this task exists so a deployment that prefers Celery Beat to a
    separate process has the same behaviour available, and so an operator can
    trigger a drain by hand during an incident.
    """
    return outbox.drain(limit)


@shared_task(queue="default")
def purge_outbox() -> int:
    """Delete delivered events past the retention window. Scheduled daily."""
    return outbox.purge_processed()


@shared_task(queue="default")
def run_fanout(limit: int = 50) -> dict[str, int]:
    """Stage two of ADR-A25: deliver claimed chunks.

    Frequent and small. The outbox plans chunks in milliseconds; this is where
    the fifty thousand rows actually get written, and it has to be able to fall
    behind without holding anything else up.
    """
    from .services import fanout

    return fanout.run_due(limit=limit)


@shared_task(queue="default")
def deliver_webhooks(limit: int = 100) -> dict[str, int]:
    """Attempt every webhook delivery whose retry time has come."""
    from .services import webhooks

    return webhooks.deliver_due(limit=limit)


@shared_task(queue="email")
def send_digests(frequency: str = "weekly") -> dict[str, int]:
    """The weekly digest (SRS §13.5).

    Everything unread, in one message, for people who asked for a digest rather
    than a stream. Sent on its own queue because a digest run is thousands of
    messages at once and must not sit in front of a password reset.
    """
    from .services import digests

    return digests.run(frequency=frequency)
