"""
The consumers that turn events into delivery (SRS §13.3, §13.4).

Two, both registered against `*`, both doing the same small thing: plan work
and return. Neither delivers inline.

That is the rule from DEVELOPER.md §4.5 — a consumer that does slow work
enqueues a task — and here it is also what makes ADR-A25 work. Planning is one
`MIN`/`MAX` and a handful of inserts regardless of whether the audience is one
person or fifty thousand; delivering is what has to be chunked, retried and
kept off the outbox's critical path.
"""

from __future__ import annotations

import logging

from .consumers import subscribe
from .models import OutboxEvent

logger = logging.getLogger(__name__)


@subscribe("*")
def plan_fanout(event: OutboxEvent) -> None:
    """Stage one of A25: split the audience into chunks.

    Idempotent — an event that already has chunks is skipped — so an outbox
    retry after a partially failed batch does not double the work.
    """
    from .services import fanout

    chunks = fanout.plan(event)
    if chunks:
        logger.info("planned fan-out", extra={"event_type": event.type, "chunks": chunks})


@subscribe("*")
def queue_webhooks(event: OutboxEvent) -> None:
    """Queue this event to every company endpoint that wants it.

    Separate from the fan-out planner because the audience is different in kind:
    a webhook goes to a company's endpoint, not to a person, and it is governed
    by a subscription list rather than by anybody's notification preferences.
    """
    from .services import webhooks

    queued = webhooks.dispatch(event)
    if queued:
        logger.info("queued webhooks", extra={"event_type": event.type, "endpoints": queued})
