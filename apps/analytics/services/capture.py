"""
Recording facts worth counting (SRS §16.1).

**Never inline, never load-bearing.** Capture happens from the outbox consumer,
after the transaction that caused it has committed, and every function here
swallows its own failures. Analytics that could fail a job publication would be
a measurement system holding the product hostage, and the trade §5.15 states
explicitly is that this table is lossy in a way the other three are not.

Two entry points:

  * `from_event` — the wildcard outbox subscriber. Domain events are the primary
    source, so most of the funnel arrives here for free and cannot drift from
    what actually happened.
  * `record` — explicit calls for interface-only facts (a search performed, a
    filter used, a template previewed). Those have no domain event because
    nothing in the domain changed, and inventing one so analytics could listen
    would put a fake state change in the outbox.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..models import AnalyticsEvent

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company
    from apps.notifications.models import OutboxEvent

logger = logging.getLogger(__name__)

#: Keys never copied out of an event payload, whatever an emitter puts there.
#:
#: An allow-list would be better and is not possible: every domain owns its own
#: payload shape and they change. So this is the block-list of things that must
#: not reach a table with a different retention from the record they came from —
#: and the payloads themselves are already required to carry names of fields
#: rather than their values (DEVELOPER.md §4.4).
NEVER_COPIED = frozenset(
    {
        "email",
        "phone",
        "content",
        "body",
        "text",
        "answer",
        "answer_text",
        "message",
        "summary",
        "rationale",
        "cv_text",
        "letter",
        "raw_output",
        "token",
        "secret",
    }
)

#: How many payload keys are kept. A payload with forty keys is a payload
#: somebody is using as a log line, and copying it wholesale would make this
#: table the largest thing in the database within a month.
MAX_PROPS = 12


def record(
    name: str,
    *,
    actor: User | None = None,
    company: Company | None = None,
    anon_id: str = "",
    props: dict[str, Any] | None = None,
) -> AnalyticsEvent | None:
    """Record one fact. Returns `None` if it could not be recorded.

    Never raises. A caller that had to handle an analytics failure would either
    ignore it — making the `try` decorative — or fail the user's action over a
    counter, which is worse than the missing count.
    """
    try:
        return AnalyticsEvent.objects.create(
            name=name[:80],
            actor=actor,
            company=company,
            anon_id=(anon_id or "")[:64],
            props=_clean(props),
        )
    except Exception:  # noqa: BLE001 - a counter must never break the product
        logger.exception("analytics capture failed", extra={"event": name})
        return None


def from_event(event: OutboxEvent) -> AnalyticsEvent | None:
    """Record one domain event (§16.1).

    Domain events are the primary source, which is what keeps the funnel honest:
    the number of applications counted here is the number of `application.submitted`
    events the outbox delivered, and there is no second path that could disagree.
    """
    payload = event.payload or {}
    return record(
        event.type,
        company=_company_for(payload),
        actor=None,
        props=payload,
    )


# --- internals ------------------------------------------------------------


def _clean(props: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the countable, drop the personal.

    Values are coerced to primitives: a nested payload copied wholesale is how
    a resume ends up in an analytics table with a different retention from the
    resume.
    """
    source = props or {}
    kept: dict[str, Any] = {}

    for key, value in source.items():
        if len(kept) >= MAX_PROPS:
            break
        if key in NEVER_COPIED or any(banned in key for banned in NEVER_COPIED):
            continue
        if isinstance(value, bool | int | float):
            kept[key] = value
        elif isinstance(value, str):
            # Truncated hard: an identifier or a status is short, and anything
            # long is prose that does not belong here.
            kept[key] = value[:120]
        elif isinstance(value, list):
            kept[key] = len(value)

    return kept


def _company_for(payload: dict[str, Any]) -> Company | None:
    """Resolve the company an event belongs to, if it names one.

    Denormalised onto the row so a company's rollup is one indexed scan rather
    than a join through whatever object the event was about.
    """
    from apps.companies.models import Company

    slug = payload.get("company_slug")
    company_id = payload.get("company_id")

    try:
        if slug:
            return Company.objects.filter(slug=slug).first()
        if company_id:
            return Company.objects.filter(pk=company_id).first()
    except Exception:  # noqa: BLE001 - same rule: never break the caller
        logger.exception("analytics company lookup failed")
    return None
