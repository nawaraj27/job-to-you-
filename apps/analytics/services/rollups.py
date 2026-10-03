"""
Nightly rollups (SRS §16.1).

**Idempotent per (date, dimension).** Every function here uses
`update_or_create` keyed on the day and its dimension, so a re-run — after a
crash, after a backfill, after somebody runs it twice by hand — recomputes the
same row rather than adding a second one. A rollup that double-counted on a
retry would be a dashboard nobody could trust after the first incident.

**Computed from the domain tables, not from `AnalyticsEvent`.** That is
deliberate and worth stating, because §16.1 says analytics events are the
primary *source* and it would be easy to read that as "count the events". The
events are lossy by design; applications, stage moves and AI runs are not. A
funnel counted from events would drift from the funnel counted from
applications, and the second one is the true one.

`AnalyticsEvent` is what answers questions nobody thought to build a table for —
which is the job it is good at, and the job the rollups are bad at.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.utils import timezone

from ..models import AICostDaily, CompanyDailyStat, FunnelSnapshot, JobDailyStat

logger = logging.getLogger(__name__)

#: The stage that means somebody was hired. A key rather than a kind: `hired`
#: and `declined` are both `terminal`, which is the right modelling — both end
#: the application — and the wrong thing to count on.
HIRED_STAGE_KEY = "hired"


def run_all(*, day: dt.date | None = None) -> dict[str, int]:
    """Every rollup for one day. What the nightly task calls.

    Yesterday by default, not today: a day that is still happening produces a
    row that is wrong until midnight, and a dashboard showing a partial day
    beside complete ones invites the wrong conclusion every morning.
    """
    day = day or (timezone.localdate() - dt.timedelta(days=1))
    return {
        "jobs": roll_jobs(day=day),
        "companies": roll_companies(day=day),
        "funnels": roll_funnels(day=day),
        "ai": roll_ai_cost(day=day),
    }


def roll_jobs(*, day: dt.date) -> int:
    """Views, applications and shortlists per posting (§16.2)."""
    from apps.applications.models import Application, StageKind
    from apps.jobs.models import JobView

    start, end = _bounds(day)
    written = 0

    views = dict(
        JobView.objects.filter(created_at__gte=start, created_at__lt=end)
        .values_list("job_id")
        .annotate(total=Count("id"))
    )
    unique = dict(
        JobView.objects.filter(created_at__gte=start, created_at__lt=end)
        .values_list("job_id")
        .annotate(total=Count("viewer_hash", distinct=True))
    )
    applications = (
        Application.objects.filter(applied_at__gte=start, applied_at__lt=end)
        .values("job_id")
        .annotate(
            total=Count("id"),
            shortlisted=Count("id", filter=Q(stage__kind=StageKind.INTERVIEW)),
            rejected=Count("id", filter=Q(closed_at__isnull=False)),
        )
    )
    by_job: dict[int, Any] = {row["job_id"]: row for row in applications}

    for job_id in set(views) | set(by_job):
        row = by_job.get(job_id, {})
        JobDailyStat.objects.update_or_create(
            job_id=job_id,
            day=day,
            defaults={
                "views": views.get(job_id, 0),
                "unique_viewers": unique.get(job_id, 0),
                "applications": row.get("total", 0),
                "shortlisted": row.get("shortlisted", 0),
                "rejected": row.get("rejected", 0),
                "computed_at": timezone.now(),
            },
        )
        written += 1

    # A posting with no activity gets no row rather than a row of zeroes: the
    # dashboard reads "no data" the same as "all zeroes", and one of them costs
    # a row per posting per day forever.
    _prune_missing(JobDailyStat, day=day, seen=set(views) | set(by_job), field="job_id")
    return written


def roll_companies(*, day: dt.date) -> int:
    """One company's day, including the response measurement (§16.3.3)."""
    from apps.applications.models import Application, ApplicationEvent

    start, end = _bounds(day)
    written = 0

    arrived = (
        Application.objects.filter(applied_at__gte=start, applied_at__lt=end)
        .values("job__company_id")
        .annotate(total=Count("id"))
    )
    counts = {row["job__company_id"]: row["total"] for row in arrived}

    # A response is the first move out of `new`. Not a message and not a view:
    # what a candidate is waiting for is a decision, and the thing they complain
    # about is never getting one.
    moves = ApplicationEvent.objects.filter(
        created_at__gte=start,
        created_at__lt=end,
        from_stage__key="new",
    ).select_related("application", "application__job")

    responded: dict[int, int] = {}
    hours: dict[int, Decimal] = {}
    for move in moves:
        company_id = move.application.job.company_id
        responded[company_id] = responded.get(company_id, 0) + 1
        waited = move.created_at - move.application.applied_at
        hours[company_id] = hours.get(company_id, Decimal("0")) + Decimal(
            f"{waited.total_seconds() / 3600:.2f}"
        )

    outcomes = (
        Application.objects.filter(last_activity_at__gte=start, last_activity_at__lt=end)
        .values("job__company_id")
        .annotate(
            # Aliased away from the reverse accessor names: `offers` is already
            # a relation on `Application`, and an annotation that shadows one is
            # a query Django refuses in a way nobody enjoys debugging.
            interview_count=Count("interview_sessions", distinct=True),
            offer_count=Count("offers", distinct=True),
            # By stage **key**, not kind. `hired` and `declined` share the
            # `terminal` kind, so filtering on kind would count every rejection
            # as a hire — or, filtering on a kind named "hired" that does not
            # exist, count none at all.
            hire_count=Count("id", filter=Q(stage__key=HIRED_STAGE_KEY), distinct=True),
        )
    )
    by_company: dict[int, Any] = {row["job__company_id"]: row for row in outcomes}

    for company_id in set(counts) | set(responded) | set(by_company):
        row = by_company.get(company_id, {})
        CompanyDailyStat.objects.update_or_create(
            company_id=company_id,
            day=day,
            defaults={
                "applications": counts.get(company_id, 0),
                "responded": responded.get(company_id, 0),
                "response_hours_total": hours.get(company_id, Decimal("0")),
                "interviews": row.get("interview_count", 0),
                "offers": row.get("offer_count", 0),
                "hires": row.get("hire_count", 0),
                "computed_at": timezone.now(),
            },
        )
        written += 1
    return written


def roll_funnels(*, day: dt.date) -> int:
    """Where everybody is, per company, per stage (§16.2)."""
    from apps.applications.models import Application, ApplicationEvent

    start, end = _bounds(day)
    written = 0

    standing = (
        Application.objects.filter(withdrawn_at__isnull=True, closed_at__isnull=True)
        .values("job__company_id", "stage__key", "stage__order")
        .annotate(total=Count("id"))
    )

    entered: dict[tuple[int, str], int] = {}
    exited: dict[tuple[int, str], int] = {}
    dwell: dict[tuple[int, str], Decimal] = {}

    for move in ApplicationEvent.objects.filter(
        created_at__gte=start, created_at__lt=end, to_stage__isnull=False
    ).select_related("application__job", "from_stage", "to_stage"):
        company_id = move.application.job.company_id
        to_stage = move.to_stage
        from_stage = move.from_stage
        if to_stage is None:
            continue

        entered[(company_id, to_stage.key)] = entered.get((company_id, to_stage.key), 0) + 1
        if from_stage is not None:
            key = (company_id, from_stage.key)
            exited[key] = exited.get(key, 0) + 1
            # How long they sat there, from the previous move into that stage.
            since = _entered_at(move)
            if since is not None:
                waited = move.created_at - since
                dwell[key] = dwell.get(key, Decimal("0")) + Decimal(
                    f"{waited.total_seconds() / 3600:.2f}"
                )

    seen: set[tuple[int, str]] = set()
    for row in standing:
        key = (row["job__company_id"], row["stage__key"])
        seen.add(key)
        FunnelSnapshot.objects.update_or_create(
            company_id=row["job__company_id"],
            day=day,
            stage_key=row["stage__key"],
            defaults={
                "stage_order": row["stage__order"] or 0,
                "count": row["total"],
                "entered": entered.get(key, 0),
                "exited": exited.get(key, 0),
                "hours_in_stage_total": dwell.get(key, Decimal("0")),
                "computed_at": timezone.now(),
            },
        )
        written += 1

    # A stage everybody left still had movement worth recording, even though
    # nobody stands in it today.
    for key in set(entered) | set(exited):
        if key in seen:
            continue
        company_id, stage_key = key
        FunnelSnapshot.objects.update_or_create(
            company_id=company_id,
            day=day,
            stage_key=stage_key,
            defaults={
                "count": 0,
                "entered": entered.get(key, 0),
                "exited": exited.get(key, 0),
                "hours_in_stage_total": dwell.get(key, Decimal("0")),
                "computed_at": timezone.now(),
            },
        )
        written += 1

    return written


def roll_ai_cost(*, day: dt.date) -> int:
    """What AI cost, per task (§16.2, platform view)."""
    from apps.ai.models import AIRun, RunStatus

    start, end = _bounds(day)
    rows = (
        AIRun.objects.filter(created_at__gte=start, created_at__lt=end)
        .values("task_key")
        .annotate(
            runs=Count("id"),
            failures=Count("id", filter=~Q(status=RunStatus.OK)),
            cached=Count("id", filter=Q(cached_from__isnull=False)),
            tokens_in=Sum("tokens_in"),
            tokens_out=Sum("tokens_out"),
            cost=Sum("cost"),
            latency=Sum("latency_ms"),
        )
    )

    written = 0
    for row in rows:
        AICostDaily.objects.update_or_create(
            day=day,
            task_key=row["task_key"],
            defaults={
                "runs": row["runs"],
                "failures": row["failures"],
                "cached": row["cached"],
                "tokens_in": row["tokens_in"] or 0,
                "tokens_out": row["tokens_out"] or 0,
                "cost": row["cost"] or Decimal("0"),
                "latency_ms_total": row["latency"] or 0,
                "computed_at": timezone.now(),
            },
        )
        written += 1
    return written


def backfill(*, days: int = 30, until: dt.date | None = None) -> dict[str, int]:
    """Recompute a range. Safe to run repeatedly.

    The reason this exists rather than being a one-off script: a rollup bug is
    found *after* it has been writing wrong numbers for a fortnight, and the fix
    is worthless without a way to correct what it already wrote.
    """
    until = until or (timezone.localdate() - dt.timedelta(days=1))
    totals = {"jobs": 0, "companies": 0, "funnels": 0, "ai": 0}

    for offset in range(days):
        day = until - dt.timedelta(days=offset)
        for key, value in run_all(day=day).items():
            totals[key] += value

    return totals


# --- internals ------------------------------------------------------------


def _bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """The day, in the platform's timezone, as an aware half-open range."""
    start = timezone.make_aware(dt.datetime.combine(day, dt.time.min))
    return start, start + dt.timedelta(days=1)


def _entered_at(move: Any) -> dt.datetime | None:
    """When this application entered the stage it is now leaving.

    The previous move *into* that stage, or the application date when the stage
    it is leaving is the first one. Without the second case, time-in-stage for
    `new` would always be missing — and `new` is the stage the whole response
    measurement is about.
    """
    from apps.applications.models import ApplicationEvent

    previous = (
        ApplicationEvent.objects.filter(
            application_id=move.application_id,
            to_stage_id=move.from_stage_id,
            created_at__lt=move.created_at,
        )
        .order_by("-created_at")
        .first()
    )
    if previous is not None:
        return previous.created_at
    return move.application.applied_at


def _prune_missing(model: Any, *, day: dt.date, seen: set[int], field: str) -> None:
    """Delete rows for dimensions that had no activity on a recomputed day.

    Only matters on a re-run: if yesterday's job had views and a correction
    removes them, the stale row would otherwise sit there claiming they
    happened.
    """
    model.objects.filter(day=day).exclude(**{f"{field}__in": list(seen)}).delete()
