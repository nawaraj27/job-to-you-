"""
Metered usage (SRS §5.14).

One counter per (owner, metric, period). Incremented in the database with `F()`
rather than read-then-written, because two exports finishing together must not
lose one another's count — the same race that makes a budget leak in
`ai.services.quotas`, and the same answer.

Counters are **consumed after the work succeeds**, never before. A candidate
whose analysis failed on a provider outage has not used their monthly analysis,
and charging them for it would be the platform's bug billed to the user.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F

from ..models import UsageCounter, UsagePeriod


def current(*, owner: Any, metric: str, period: str = UsagePeriod.MONTH) -> UsageCounter:
    """This period's counter, created on first use."""
    counter, _ = UsageCounter.objects.get_or_create(
        owner_ct=ContentType.objects.get_for_model(owner),
        owner_id=str(owner.pk),
        metric=metric,
        period=period,
        period_start=UsageCounter.start_of(period),
        defaults={"value": 0},
    )
    return counter


def used(*, owner: Any, metric: str, period: str = UsagePeriod.MONTH) -> int:
    """How much of this period's allowance is gone.

    A read that does not create a row: most accounts never touch most metrics,
    and a counter table with a row per account per metric per month for people
    who used none of it is a table nobody can read.
    """
    row = (
        UsageCounter.objects.filter(
            owner_ct=ContentType.objects.get_for_model(owner),
            owner_id=str(owner.pk),
            metric=metric,
            period=period,
            period_start=UsageCounter.start_of(period),
        )
        .values_list("value", flat=True)
        .first()
    )
    return row or 0


@transaction.atomic
def record(*, owner: Any, metric: str, period: str = UsagePeriod.MONTH, amount: int = 1) -> int:
    """Consume `amount` of an allowance. Returns the new total."""
    counter = current(owner=owner, metric=metric, period=period)
    UsageCounter.objects.filter(pk=counter.pk).update(value=F("value") + amount)
    counter.refresh_from_db(fields=["value"])
    return counter.value


@transaction.atomic
def take(*, owner: Any, metric: str, allowed: int, period: str = UsagePeriod.MONTH) -> bool:
    """Claim one unit of a limited allowance, atomically. True when granted.

    The one operation that is safe against a double-click, and the reason it
    exists: `require()` reads the counter and `consume()` increments it, with
    the work in between. Two requests arriving together both read `used=0`,
    both pass, and a free account gets two of something it is allowed one of.
    The increment was always atomic; the *decision* was not.

    `SELECT … FOR UPDATE` on the counter row serialises the decision. The row
    is per (owner, metric, period), so the lock is held for a single
    increment and contended only by the same account doing the same thing at
    the same instant — which is exactly the case being defended against.
    """
    counter = current(owner=owner, metric=metric, period=period)

    locked = UsageCounter.objects.select_for_update().get(pk=counter.pk)
    if locked.value >= allowed:
        return False

    UsageCounter.objects.filter(pk=locked.pk).update(value=F("value") + 1)
    return True


@transaction.atomic
def release(*, owner: Any, metric: str, period: str = UsagePeriod.MONTH) -> int:
    """Give one unit back after the work it was claimed for failed.

    Floored at zero rather than allowed to go negative: a refund that arrives
    twice — a retry, a race with the prune — must not hand out free allowance.
    """
    counter = current(owner=owner, metric=metric, period=period)
    UsageCounter.objects.filter(pk=counter.pk, value__gt=0).update(value=F("value") - 1)
    counter.refresh_from_db(fields=["value"])
    return counter.value


def history(*, owner: Any, metric: str, months: int = 6) -> list[UsageCounter]:
    """Recent periods, newest first. What a usage screen shows."""
    return list(
        UsageCounter.objects.filter(
            owner_ct=ContentType.objects.get_for_model(owner),
            owner_id=str(owner.pk),
            metric=metric,
        ).order_by("-period_start")[:months]
    )


def prune(*, before: dt.date) -> int:
    """Delete counters for periods nobody can still be inside.

    Kept for a while rather than deleted on rollover, because "why was I
    refused last month" is a support question, and a counter that vanished at
    midnight cannot answer it.
    """
    deleted, _ = UsageCounter.objects.filter(period_start__lt=before).delete()
    return deleted
