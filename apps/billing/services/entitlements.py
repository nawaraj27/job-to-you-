"""
What an account is allowed to do (SRS §5.14, P18's exit criterion).

**One function answers every commercial question, and no template asks one.**
`require()` raises; `allows()` reports. A view may call `allows()` to decide
whether to render an upgrade prompt, but the refusal itself always happens in a
service, because a limit enforced in a template is a limit that stops applying
the moment somebody posts the form directly.

**Every account has a plan.** An owner with no subscription resolves to the
default plan for its audience, so there is no "unsubscribed" branch anywhere in
the enforcement path — which is exactly where a missing-plan bug would live,
quietly allowing everything.

**A live subscription includes `past_due`.** Cutting somebody off the hour a
card fails loses the customer and the payment. Dunning belongs to
`subscriptions.expire_overdue`, on a schedule somebody chose.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.common.errors import QuotaExceeded

from .. import limits as limit_registry
from ..models import LIMITS_SCHEMA_VERSION, Plan, PlanAudience, Subscription
from . import usage


class PlanLimitExceeded(QuotaExceeded):
    """The plan says no. Distinct from a rate limit, and differently fixable.

    402 rather than 429: this is not "slow down", it is "this costs money".
    A person who retries in a minute will be refused again, and a status that
    tells them to wait would be a lie told by the framework.
    """

    status_code = 402
    code = "plan_limit"
    default_message = "That is not included in your plan."


def plan_for(owner: Any) -> Plan:
    """The plan governing this owner. Never `None`."""
    subscription = live_subscription(owner)
    if subscription is not None:
        return subscription.plan
    return default_plan(audience_of(owner))


def live_subscription(owner: Any) -> Subscription | None:
    """The one subscription conferring entitlements, if there is one.

    Singular by database constraint, not by `.first()` hoping: two live
    subscriptions would make "which plan am I on" a question with two answers.
    """
    candidate, company = _owner_columns(owner)
    if candidate is None and company is None:
        return None

    query = Subscription.objects.live().select_related("plan")
    query = query.filter(candidate=candidate) if candidate else query.filter(company=company)
    return query.first()


def limits_for(owner: Any) -> dict[str, Any]:
    """The effective limit document: the plan's, over the declared defaults."""
    return read_limits(plan_for(owner).limits)


def value_of(owner: Any, key: str) -> Any:
    """One limit's effective value. `None` means unlimited."""
    limit = limit_registry.get(key)
    return limits_for(owner).get(key, limit.default)


def allows(owner: Any, key: str, *, value: Any = None) -> bool:
    """Whether this owner may do the thing, without raising.

    For a set limit, `value` is the member being asked about — "may I export
    docx" rather than "may I export".
    """
    limit = limit_registry.get(key)
    allowed = value_of(owner, key)

    if limit.kind == "flag":
        return bool(allowed)
    if limit.kind == "set":
        if allowed is None:
            return True
        return value in list(allowed)
    if limit.kind == "count":
        if allowed is None:
            return True
        return usage.used(owner=meter_owner(owner), metric=key, period=limit.period) < allowed
    return True


def require(owner: Any, key: str, *, value: Any = None) -> None:
    """Refuse, in the plan's own words, or return quietly."""
    if not allows(owner, key, value=value):
        limit = limit_registry.get(key)
        raise PlanLimitExceeded(
            limit.refusal,
            details={
                "limit": key,
                "plan": plan_for(owner).code,
                "allowed": value_of(owner, key),
            },
        )


@transaction.atomic
def claim(owner: Any, key: str) -> None:
    """Check *and* consume one metered unit in a single atomic step.

    Use this, not `require` + `consume`, wherever the work between them can be
    started twice. `require` reads and `consume` increments; two requests
    arriving together both pass the read, and a free account gets two of
    something it is allowed one of. A double-click is enough to do it.

    An unlimited allowance still records usage — the counter is what the usage
    screen and any later question about behaviour both read.
    """
    limit = limit_registry.get(key)
    if limit.kind != "count":
        raise ValueError(f"{key} is not metered; there is nothing to claim.")

    allowed = value_of(owner, key)
    if allowed is None:
        consume(owner, key)
        return

    if not usage.take(
        owner=meter_owner(owner), metric=key, allowed=int(allowed), period=limit.period
    ):
        used = usage.used(owner=meter_owner(owner), metric=key, period=limit.period)
        reset = resets_on(key)
        raise PlanLimitExceeded(
            refusal_for(limit, used=used, allowed=int(allowed), reset=reset),
            details={
                "limit": key,
                "plan": plan_for(owner).code,
                "allowed": allowed,
                "used": used,
                "resets_on": reset.isoformat(),
            },
        )

    _announce_if_exhausted(owner, key, allowed)


def resets_on(key: str, *, today: dt.date | None = None) -> dt.date:
    """The day a metered allowance starts again: the day enforcement uses.

    Derived from the same period start `usage.take` counts against, so the date
    on screen and the date the counter actually resets cannot disagree.
    """
    from ..models import UsageCounter, UsagePeriod

    limit = limit_registry.get(key)
    today = today or timezone.localdate()
    start = UsageCounter.start_of(limit.period, today=today)
    if limit.period == UsagePeriod.DAY:
        return start + dt.timedelta(days=1)
    return (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def refusal_for(limit: Any, *, used: int, allowed: int, reset: dt.date) -> str:
    """The sentence somebody reads at a limit: what, how much, until when."""
    when = f"{reset.day} {reset:%B %Y}"
    return (
        f"{limit.refusal} You have used {used} of {allowed}. "
        f"Your allowance resets on {when}. Pro includes more."
    )


def release(owner: Any, key: str) -> None:
    """Return a claimed unit after the work it paid for failed.

    A candidate whose analysis died on a provider outage has not used their
    monthly analysis. Claiming first and releasing on failure keeps both
    properties that matter: a double-click cannot spend twice, and the
    platform's own failure does not cost the reader anything.
    """
    limit = limit_registry.get(key)
    if limit.kind != "count":
        raise ValueError(f"{key} is not metered; there is nothing to release.")
    usage.release(owner=meter_owner(owner), metric=key, period=limit.period)


def _announce_if_exhausted(owner: Any, key: str, allowed: int) -> None:
    """Emit `subscription.limit_reached` on the crossing, once."""
    meter = meter_owner(owner)
    period = limit_registry.get(key).period
    if usage.used(owner=meter, metric=key, period=period) != allowed:
        return

    from apps.notifications import events

    events.emit(
        "subscription.limit_reached",
        meter,
        payload={
            "owner": f"{meter._meta.model_name}:{meter.pk}",
            "limit": key,
            "allowed": allowed,
        },
    )


def consume(owner: Any, key: str, *, amount: int = 1) -> int:
    """Record use of a metered allowance. Call **after** the work succeeded.

    Emits `subscription.limit_reached` on the crossing, and only on the
    crossing: once when the counter first reaches the allowance, never on the
    refusals that follow. An event per refusal would mail somebody about their
    plan every time they clicked.
    """
    limit = limit_registry.get(key)
    if limit.kind != "count":
        raise ValueError(f"{key} is not metered; there is nothing to consume.")

    meter = meter_owner(owner)
    total = usage.record(owner=meter, metric=key, period=limit.period, amount=amount)

    allowed = value_of(owner, key)
    if allowed is not None and total - amount < allowed <= total:
        from apps.notifications import events

        events.emit(
            "subscription.limit_reached",
            meter,
            payload={
                "owner": f"{meter._meta.model_name}:{meter.pk}",
                "limit": key,
                "allowed": allowed,
            },
        )
    return total


def remaining(owner: Any, key: str) -> int | None:
    """How much is left, or `None` for unlimited. What a usage screen shows."""
    limit = limit_registry.get(key)
    allowed = value_of(owner, key)
    if limit.kind != "count" or allowed is None:
        return None
    return max(allowed - usage.used(owner=meter_owner(owner), metric=key, period=limit.period), 0)


def money(owner: Any, key: str, *, fallback: Decimal) -> Decimal:
    """A money limit as a `Decimal`, falling back when the plan is silent.

    The fallback is the caller's existing default — `config/ai.py` for AI
    spend — so a plan that says nothing about money leaves the code's own
    ceiling in place rather than removing it (ADR-A28).
    """
    raw = value_of(owner, key)
    return fallback if raw is None else Decimal(str(raw))


def meter_owner(owner: Any) -> Any:
    """Which row a counter hangs off.

    The subscription's owner, so that usage and entitlement cannot disagree
    about who is being metered. A user is resolved to their profile for the
    same reason.
    """
    candidate, company = _owner_columns(owner)
    resolved = company or candidate
    if resolved is None:
        raise ValueError(
            "Nothing to meter: this account has no candidate profile and is not a company."
        )
    return resolved


def audience_of(owner: Any) -> str:
    from apps.companies.models import Company

    return PlanAudience.COMPANY if isinstance(owner, Company) else PlanAudience.CANDIDATE


def default_plan(audience: str) -> Plan:
    """The plan an owner has when they have none.

    Missing rather than absent is a deployment error, not a runtime state: the
    seed command creates it, and a system with no default plan cannot answer
    any entitlement question at all.
    """
    plan = Plan.objects.filter(audience=audience, is_default=True, is_active=True).first()
    if plan is None:
        raise LookupError(
            f"No default {audience} plan exists. Run `manage.py seed_plans` — "
            "entitlements cannot be resolved without one."
        )
    return plan


# --- internals ------------------------------------------------------------


def _owner_columns(owner: Any) -> tuple[Any, Any]:
    """`(candidate, company)`, at most one of which is set."""
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company

    if isinstance(owner, Company):
        return None, owner
    if isinstance(owner, CandidateProfile):
        return owner, None
    # A user: their profile is the billable party. Someone with no candidate
    # profile — an employer-only account — has no candidate subscription, and
    # falls through to the default plan rather than being an error.
    return getattr(owner, "candidate_profile", None), None


def read_limits(document: dict[str, Any]) -> dict[str, Any]:
    """Read a plan document at whatever version it was written (ADR-A27).

    One branch today. The dispatch exists now so that the second version has a
    place to go other than an `if` somebody adds in a hurry.
    """
    version = int(document.get("schema_version", LIMITS_SCHEMA_VERSION))
    if version != LIMITS_SCHEMA_VERSION:
        raise ValueError(
            f"Plan limits at schema version {version}; this build reads "
            f"{LIMITS_SCHEMA_VERSION}. Add an upgrade branch before deploying."
        )

    effective = {key: limit.default for key, limit in limit_registry.REGISTRY.items()}
    effective.update({key: value for key, value in document.items() if key != "schema_version"})
    return effective
