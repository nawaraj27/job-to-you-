"""
Event registry and the emit API (SRS §13.1, DEVELOPER.md §4).

Every event is declared before it can be emitted. Declaration is not
bureaucracy: it is what makes the catalogue in the SRS checkable against the
code, and what stops a payload silently changing shape under a consumer.

    register("file.scan_failed", schema_version=1,
             required={"attachment_public_id", "filename", "scan_status"})

    events.emit("file.scan_failed", attachment, payload={...})

`emit` refuses to run outside a transaction. That is the whole guarantee of the
outbox pattern — an event written outside the transaction that produced it can
be delivered for a change that never committed — so it is enforced rather than
documented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction

from .models import OutboxEvent


@dataclass(frozen=True)
class EventSpec:
    type: str
    schema_version: int
    required: frozenset[str] = field(default_factory=frozenset)
    description: str = ""
    #: Which notification preference governs this event (SRS §13.1). Blank for
    #: events that are not delivered to a person — an internal fact a consumer
    #: reacts to has nothing for anybody to mute.
    preference_group: str = ""
    #: Which audience receives it (SRS §13.3). A key into the audience registry,
    #: resolved to a *query* at fan-out time (ADR-A25) — never a stored list.
    audience: str = ""
    #: How it reads in a feed or an email. Format strings over the payload, so
    #: the wording of a notification lives beside the event that causes it
    #: rather than in whichever consumer happened to build the row.
    title: str = ""
    body: str = ""
    #: Collapses a burst. Format string over the payload; blank means no
    #: collapsing (every occurrence is its own line).
    dedupe: str = ""


_REGISTRY: dict[str, EventSpec] = {}

#: The groups a person can actually toggle. Kept as a literal tuple rather than
#: read from the model, because `register()` runs at import time and reading a
#: TextChoices at import is fine but reading the database is not.
_PREFERENCE_GROUPS = (
    "job_matches",
    "application_updates",
    "employer_messages",
    "cv_insights",
    "weekly_digest",
    "product_news",
    "company_updates",
)


class UnregisteredEvent(LookupError):
    pass


class InvalidEventPayload(ValueError):
    pass


def register(
    event_type: str,
    *,
    schema_version: int = 1,
    required: set[str] | frozenset[str] = frozenset(),
    description: str = "",
    preference_group: str = "",
    audience: str = "",
    title: str = "",
    body: str = "",
    dedupe: str = "",
) -> EventSpec:
    """Declare an event type. Re-registering the same spec is a no-op."""
    if not _is_past_tense_shape(event_type):
        raise ValueError(
            f"Event type {event_type!r} must be 'noun.verb_past', lowercase and dotted."
        )

    if preference_group and preference_group not in _PREFERENCE_GROUPS:
        raise ValueError(
            f"{preference_group!r} is not a notification preference group. "
            "An event mapped to a group nobody can toggle is a channel nobody can mute."
        )

    if audience and title == "":
        raise ValueError(
            f"Event {event_type!r} has an audience but no title. "
            "A notification nobody can read is not a notification."
        )

    spec = EventSpec(
        type=event_type,
        schema_version=schema_version,
        required=frozenset(required),
        description=description,
        preference_group=preference_group,
        audience=audience,
        title=title,
        body=body,
        dedupe=dedupe,
    )
    existing = _REGISTRY.get(event_type)
    if existing is not None and existing != spec:
        raise ValueError(f"Event {event_type!r} is already registered with a different spec.")
    _REGISTRY[event_type] = spec
    return spec


def registry() -> dict[str, EventSpec]:
    return dict(_REGISTRY)


def spec_for(event_type: str) -> EventSpec:
    try:
        return _REGISTRY[event_type]
    except KeyError:
        raise UnregisteredEvent(
            f"Event {event_type!r} is not registered. Declare it in its domain's events.py."
        ) from None


def emit(
    event_type: str,
    aggregate: models.Model | None = None,
    *,
    payload: dict[str, Any] | None = None,
) -> OutboxEvent:
    """Write an event to the outbox, inside the caller's transaction."""
    spec = spec_for(event_type)
    body = payload or {}

    missing = spec.required - set(body)
    if missing:
        raise InvalidEventPayload(
            f"{event_type} is missing required payload keys: {sorted(missing)}"
        )

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            f"events.emit({event_type!r}) was called outside a transaction. "
            "An event must be written in the same transaction as the state change "
            "it describes, or it can be delivered for a change that never committed."
        )

    aggregate_ct = None
    aggregate_id = ""
    if aggregate is not None:
        aggregate_ct = ContentType.objects.get_for_model(aggregate, for_concrete_model=False)
        aggregate_id = str(aggregate.pk)

    return OutboxEvent.objects.create(
        type=event_type,
        schema_version=spec.schema_version,
        aggregate_ct=aggregate_ct,
        aggregate_id=aggregate_id,
        payload=body,
    )


def _is_past_tense_shape(event_type: str) -> bool:
    """Structural check only: lowercase, one dot, no spaces.

    Whether the verb is genuinely past tense is a review question; whether it
    looks like an event rather than a command is checkable here, and catching
    'application.submit' before it ships is worth the four lines.
    """
    if event_type != event_type.lower() or " " in event_type:
        return False
    parts = event_type.split(".")
    return len(parts) == 2 and all(part.isidentifier() for part in parts)


# --- events owned by platform apps ---------------------------------------
#
# Domain events are declared in their own app's events.py. These two belong to
# infrastructure, so they live with the registry itself.

register(
    "file.scanned",
    schema_version=1,
    required={"attachment_public_id", "scan_status"},
    description=(
        "A scan finished, whatever the verdict. Domains that must wait for a clean "
        "file subscribe to this rather than polling the attachment."
    ),
)
register(
    "file.scan_failed",
    schema_version=1,
    required={"attachment_public_id", "filename", "scan_status"},
    description="An uploaded file failed or could not complete a virus scan.",
)
