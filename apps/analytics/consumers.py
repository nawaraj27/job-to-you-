"""
Capturing every domain event (SRS §16.1).

One wildcard subscriber. Domain events are the primary source, so most of the
funnel arrives here without anybody instrumenting it — and, more importantly,
the counted number cannot drift from what actually happened, because there is
only one path.

**Asynchronous by construction.** The outbox consumer runs after the
transaction that emitted the event has committed, so a slow or failing capture
cannot roll back a job publication. `capture.record` swallows its own errors on
top of that, because a counter must never break the product.
"""

from __future__ import annotations

import logging

from apps.notifications.consumers import subscribe
from apps.notifications.models import OutboxEvent

from .services import capture

logger = logging.getLogger(__name__)


@subscribe("*")
def count_everything(event: OutboxEvent) -> None:
    """Record one domain event as a countable fact.

    The wildcard is deliberate. A list of interesting events would need editing
    every time a phase adds one, and the event that turns out to matter is
    always the one nobody added.
    """
    capture.from_event(event)
