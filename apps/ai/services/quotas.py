"""
AI budgets (SRS §5.11).

**Checked before dispatch, never after.** A limit enforced once the money is
gone is an audit trail wearing a limit's name.

Two owners, both real: a candidate has a personal monthly budget, and a company
has one covering everything its team does. A run charged to a user inside a
company counts against both, because both are true — the person used it and the
company paid for it.

Spend is recorded whatever the outcome. A failed call still cost money, and a
budget that only counts successes is not a budget.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from config import ai as ai_config

from ..models import AIUsageQuota, QuotaPeriod

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company


def check(*, user: User | None = None, company: Company | None = None) -> tuple[bool, str]:
    """May this run start? Returns `(allowed, reason)`.

    A tuple rather than an exception because the caller records a refusal as an
    `AIRun` either way, and "quota exceeded" is an outcome rather than an error.
    """
    for owner, limit in _owners(user, company):
        quota = _current(owner, limit)
        if quota.is_exhausted:
            return False, f"The {_label(owner)} AI budget for this month is used up."
    return True, ""


def record(
    *,
    user: User | None = None,
    company: Company | None = None,
    cost: Decimal,
    tokens: int = 0,
) -> None:
    """Charge one run to everybody responsible for it.

    Incremented in the database rather than read-then-written: two runs
    finishing together must not lose one another's spend, which is exactly the
    race that makes a budget leak.
    """
    for owner, limit in _owners(user, company):
        quota = _current(owner, limit)
        AIUsageQuota.objects.filter(pk=quota.pk).update(
            runs=F("runs") + 1, tokens=F("tokens") + tokens, cost=F("cost") + cost
        )


def usage_for(owner: Any) -> AIUsageQuota:
    """This period's row, for a dashboard or a staff screen."""
    return _current(owner, _default_limit(owner))


def set_limit(*, owner: Any, limit: Decimal) -> AIUsageQuota:
    """Raise or lower one owner's ceiling for the current period."""
    quota = _current(owner, limit)
    quota.limit_cost = limit
    quota.save(update_fields=["limit_cost", "updated_at"])
    return quota


# --- internals ------------------------------------------------------------


def _owners(user: User | None, company: Company | None) -> list[tuple[Any, Decimal]]:
    owners: list[tuple[object, Decimal]] = []
    if user is not None:
        owners.append((user, _plan_limit(user, ai_config.DEFAULT_USER_MONTHLY_COST)))
    if company is not None:
        owners.append((company, _plan_limit(company, ai_config.DEFAULT_COMPANY_MONTHLY_COST)))
    return owners


def _plan_limit(owner: Any, fallback: Decimal) -> Decimal:
    """The ceiling this owner's plan grants, or the code's default (P18).

    The plan supplies a number; the mechanism stays here. That keeps ADR-A28
    intact — AI *configuration* is still in code — while letting a paid plan
    raise a budget without a deploy, which is the one thing a plan has to be
    able to do. A system with no plans seeded falls back to `config/ai.py`
    rather than failing, because an AI call must not depend on the billing
    catalogue being loaded.
    """
    try:
        from apps.billing.services import entitlements

        return entitlements.money(owner, "ai_cost_monthly", fallback=fallback)
    except Exception:  # noqa: BLE001 - a budget lookup must never break a run
        return fallback


def _label(owner: Any) -> str:
    return owner._meta.verbose_name


def _default_limit(owner: Any) -> Decimal:
    from apps.companies.models import Company

    fallback = (
        ai_config.DEFAULT_COMPANY_MONTHLY_COST
        if isinstance(owner, Company)
        else ai_config.DEFAULT_USER_MONTHLY_COST
    )
    return _plan_limit(owner, fallback)


def _period_start() -> dt.date:
    today = timezone.now().date()
    return today.replace(day=1)


@transaction.atomic
def _current(owner: Any, limit: Decimal) -> AIUsageQuota:
    """This period's row, created on first use.

    `get_or_create` rather than a nightly job that pre-creates rows for every
    account: most accounts never make an AI call, and a table with a row per
    account per month for people who never used it is a table nobody can read.
    """
    quota, _ = AIUsageQuota.objects.get_or_create(
        owner_ct=ContentType.objects.get_for_model(owner),
        owner_id=str(owner.pk),
        period=QuotaPeriod.MONTH,
        period_start=_period_start(),
        defaults={"limit_cost": limit},
    )
    return quota
