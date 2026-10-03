"""
Event subscribers (DEVELOPER.md §4.5).

A consumer is a plain function registered against an event type. Consumers are
isolated — one failing does not stop the others — idempotent, and fast: real
work is enqueued, not performed inline.

Phase 0 ships one consumer, which logs. It is not a placeholder: it is the
observability floor for the event system, it stays in place permanently, and it
is what proves the outbox round-trips before any domain has anything to say.
Delivery to notifications, email and webhooks arrives in P10.

    @subscribe("file.scan_failed")
    def quarantine_notice(event): ...
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

from .models import OutboxEvent

logger = logging.getLogger("careerai.events")

Consumer = Callable[[OutboxEvent], None]

_SUBSCRIBERS: dict[str, list[Consumer]] = defaultdict(list)

WILDCARD = "*"


def subscribe(*event_types: str) -> Callable[[Consumer], Consumer]:
    """Register a function against one or more event types.

    `subscribe("*")` receives everything, which is how cross-cutting concerns
    such as logging and analytics attach without naming every event.
    """

    def decorator(fn: Consumer) -> Consumer:
        for event_type in event_types:
            if fn not in _SUBSCRIBERS[event_type]:
                _SUBSCRIBERS[event_type].append(fn)
        return fn

    return decorator


def consumers_for(event_type: str) -> list[Consumer]:
    return [*_SUBSCRIBERS.get(WILDCARD, []), *_SUBSCRIBERS.get(event_type, [])]


def name_of(consumer: Consumer) -> str:
    """Stable identity for the delivery ledger.

    Module path plus qualified name, so renaming a function is a new consumer
    and will re-receive events it has not seen under its new name — which is
    the safe direction for that mistake.
    """
    return f"{consumer.__module__}.{consumer.__qualname__}"


@subscribe(WILDCARD)
def log_event(event: OutboxEvent) -> None:
    """Structured record of every event that leaves the outbox."""
    logger.info(
        "event delivered",
        extra={
            "event_type": event.type,
            "event_id": event.pk,
            "schema_version": event.schema_version,
            "aggregate": f"{event.aggregate_ct_id}:{event.aggregate_id}"
            if event.aggregate_ct_id
            else None,
            "payload_keys": sorted(event.payload),
        },
    )
