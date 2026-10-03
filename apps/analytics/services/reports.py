"""
What each dashboard reads (SRS §16.2).

**Every function here reads a rollup.** Nothing aggregates `AnalyticsEvent`, and
nothing aggregates the domain tables on the request path — the employer
dashboard has to stay fast on the day a posting goes viral, which is the one day
somebody opens it.

The exception, stated because it looks like a violation: **the candidate side
computes live.** §5.14 says so — there is no per-candidate daily rollup, because
one candidate has tens of applications rather than thousands and a nightly job
per candidate would be a table larger than the thing it summarises. The cost is
a few indexed counts on one person's own rows.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db.models import Count, Q, Sum
from django.utils import timezone

from ..models import AICostDaily, CompanyDailyStat, FunnelSnapshot, JobDailyStat
from .rollups import HIRED_STAGE_KEY

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company

#: The default reporting window. A month, because that is the period a hiring
#: manager thinks in and long enough for a funnel to have a shape.
DEFAULT_DAYS = 30


# --- employer -------------------------------------------------------------


def company_overview(company: Company, *, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """The headline numbers, from `CompanyDailyStat` (§16.2)."""
    since = timezone.localdate() - dt.timedelta(days=days)
    rows = CompanyDailyStat.objects.filter(company=company, day__gte=since)

    totals = rows.aggregate(
        applications=Sum("applications"),
        responded=Sum("responded"),
        hours=Sum("response_hours_total"),
        interviews=Sum("interviews"),
        offers=Sum("offers"),
        hires=Sum("hires"),
    )
    applications = totals["applications"] or 0
    responded = totals["responded"] or 0
    hours = float(totals["hours"] or 0)

    return {
        "days": days,
        "applications": applications,
        "responded": responded,
        "response_rate": round(responded / applications, 4) if applications else 0.0,
        "average_response_hours": round(hours / responded, 1) if responded else 0.0,
        "interviews": totals["interviews"] or 0,
        "offers": totals["offers"] or 0,
        "hires": totals["hires"] or 0,
        "offer_acceptance": _offer_acceptance(company, since=since),
        # The nudge (§16.3.3). A rate rather than a scolding: an employer who
        # replies to two in three is doing better than most, and telling them
        # they are failing would get the feature switched off.
        "response_nudge": _response_nudge(responded, applications, hours),
        "has_data": applications > 0,
        # When these numbers were last worked out. The screen said "Updated
        # overnight", which is a promise rather than a fact: if the rollup has
        # not run — a broker down, a worker not deployed — the page shows
        # yesterday's figures with today's confidence, and an employer reading
        # "0 interviews" on the same portal that shows one booked for Tuesday
        # has no way to tell which of the two is wrong.
        "counted_to": rows.order_by("-day").values_list("day", flat=True).first(),
        "computed_at": rows.order_by("-computed_at").values_list("computed_at", flat=True).first(),
    }


def company_funnel(company: Company, *, day: dt.date | None = None) -> list[dict[str, Any]]:
    """The funnel with drop-off, from the latest snapshot (§16.2).

    Drop-off is computed against the previous stage's count rather than against
    the total: "we lose two thirds between screening and interview" is the
    sentence a hiring manager can act on, and a percentage of the original
    intake is not.
    """
    day = day or _latest_day(FunnelSnapshot, company=company)
    if day is None:
        return []

    rows = list(
        FunnelSnapshot.objects.filter(company=company, day=day).order_by("stage_order", "stage_key")
    )

    funnel = []
    previous: int | None = None
    for row in rows:
        # A stage can hold more people than the one before it, because a
        # recruiter can move somebody straight from "new" to "interview" and
        # skip the middle. That is a normal thing to do and it is not a
        # negative drop-off: the screen rendered "-100% do not reach this
        # stage", which is not a sentence about anything.
        #
        # So drop-off is reported only where the funnel actually narrows, and
        # the skip is named instead. `gained` is the honest reading — more
        # people are here than passed through the stage above.
        drop = gained = None
        if previous:
            if row.count <= previous:
                drop = round(1 - (row.count / previous), 4)
            else:
                gained = row.count - previous

        funnel.append(
            {
                "stage": row.stage_key,
                "count": row.count,
                "entered": row.entered,
                "exited": row.exited,
                "average_hours": row.average_hours_in_stage,
                "drop_off": drop,
                "skipped_in": gained,
            }
        )
        previous = row.count or previous
    return funnel


def job_performance(company: Company, *, days: int = DEFAULT_DAYS) -> list[dict[str, Any]]:
    """Views → applies → shortlists, per posting (§16.2).

    Ordered by views because the question is "which posting is doing badly",
    and a posting nobody sees and a posting everybody sees and ignores are
    different problems with different fixes.
    """
    since = timezone.localdate() - dt.timedelta(days=days)
    rows = (
        JobDailyStat.objects.filter(job__company=company, day__gte=since)
        .values("job_id", "job__title", "job__slug")
        .annotate(
            views=Sum("views"),
            applications=Sum("applications"),
            shortlisted=Sum("shortlisted"),
        )
        .order_by("-views")
    )

    return [
        {
            "job_id": row["job_id"],
            "title": row["job__title"],
            "slug": row["job__slug"],
            "views": row["views"] or 0,
            "applications": row["applications"] or 0,
            "shortlisted": row["shortlisted"] or 0,
            "apply_rate": _rate(row["applications"], row["views"]),
            "shortlist_rate": _rate(row["shortlisted"], row["applications"]),
        }
        for row in rows
    ]


def time_to_hire(company: Company, *, days: int = 180) -> dict[str, Any]:
    """How long a hire takes, end to end.

    Computed from applications rather than from the funnel rollup: time-to-hire
    is a property of one application's whole life, and summing daily dwell times
    would answer a different question badly.
    """
    from apps.applications.models import Application

    since = timezone.now() - dt.timedelta(days=days)
    hired = Application.objects.filter(
        job__company=company, stage__key=HIRED_STAGE_KEY, last_activity_at__gte=since
    ).values_list("applied_at", "last_activity_at")

    spans = [(end - start).days for start, end in hired if start and end]
    if not spans:
        return {"days": 0, "sample": 0, "has_data": False}

    spans.sort()
    return {
        "days": round(sum(spans) / len(spans)),
        "median": spans[len(spans) // 2],
        "sample": len(spans),
        "has_data": True,
    }


#: FR-8.2 bands, the same thresholds the match badge uses.
QUALITY_BANDS = (("Strong", 85), ("Good", 70), ("Partial", 50), ("Stretch", 0))


def source_effectiveness(company: Company, *, days: int = DEFAULT_DAYS) -> list[dict[str, Any]]:
    """Where applicants came from, and how many of them got anywhere (§16.2).

    Computed from applications in the window, like `time_to_hire`: a property
    of each application's outcome, bounded by the window and the company.
    """
    from django.db.models import Count, Q

    from apps.applications.models import Application, ApplicationSource, StageKind

    since = timezone.now() - dt.timedelta(days=days)
    rows = (
        Application.objects.filter(job__company=company, applied_at__gte=since)
        .values("source")
        .annotate(
            # distinct: the timeline join below yields one row per event, and a
            # plain count reported an application with five events as five.
            applications=Count("pk", distinct=True),
            interviewed=Count(
                "pk",
                filter=Q(timeline__to_stage__kind=StageKind.INTERVIEW)
                | Q(stage__kind=StageKind.INTERVIEW),
                distinct=True,
            ),
            hired=Count("pk", filter=Q(stage__key=HIRED_STAGE_KEY), distinct=True),
        )
        .order_by("-applications")
    )
    labels = dict(ApplicationSource.choices)
    return [
        {
            "source": str(labels.get(row["source"], row["source"])),
            "applications": row["applications"],
            "interviewed": row["interviewed"],
            "hired": row["hired"],
            "interview_rate": _rate(row["interviewed"], row["applications"]),
        }
        for row in rows
    ]


def quality_distribution(company: Company, *, days: int = DEFAULT_DAYS) -> list[dict[str, Any]]:
    """Applicants by match band at the moment they applied (§16.2, FR-8.2, FR-8.5).

    Read from the frozen `match_snapshot`, never recomputed. Applications with no
    score (matching was unavailable when they applied) are counted as such
    rather than dropped.
    """
    from apps.applications.models import Application

    since = timezone.now() - dt.timedelta(days=days)
    counts = {label: 0 for label, _ in QUALITY_BANDS}
    unscored = 0
    for snapshot in Application.objects.filter(
        job__company=company, applied_at__gte=since
    ).values_list("match_snapshot", flat=True):
        score = (snapshot or {}).get("score")
        if not isinstance(score, int | float):
            unscored += 1
            continue
        label = next(label for label, floor in QUALITY_BANDS if score >= floor)
        counts[label] += 1
    rows = [{"band": label, "count": counts[label]} for label, _ in QUALITY_BANDS]
    if unscored:
        rows.append({"band": "Not scored", "count": unscored})
    return rows


def recruiter_workload(company: Company, *, days: int = DEFAULT_DAYS) -> list[dict[str, Any]]:
    """Who moved applicants and wrote notes in the window (§16.2).

    From the application timeline and notes, so it counts what people did, not
    what they were assigned. Removed members still appear under their name:
    the work happened.
    """
    from django.db.models import Count

    from apps.applications.models import ActorKind, ApplicationEvent, ApplicationNote

    since = timezone.now() - dt.timedelta(days=days)
    moves = (
        ApplicationEvent.objects.filter(
            application__job__company=company,
            created_at__gte=since,
            actor_kind=ActorKind.EMPLOYER,
            type="application.stage_changed",
            actor__isnull=False,
        )
        .values("actor_id", "actor__full_name", "actor__email")
        .annotate(count=Count("pk"))
    )
    notes = dict(
        ApplicationNote.objects.filter(
            application__job__company=company, created_at__gte=since, author__isnull=False
        )
        .values("author_id")
        .annotate(count=Count("pk"))
        .values_list("author_id", "count")
    )
    rows = {
        row["actor_id"]: {
            "name": row["actor__full_name"] or row["actor__email"].split("@")[0],
            "moves": row["count"],
            "notes": notes.pop(row["actor_id"], 0),
        }
        for row in moves
    }
    if notes:
        from apps.accounts.models import User

        for user in User.objects.filter(pk__in=list(notes)):
            rows[user.pk] = {"name": user.get_full_name(), "moves": 0, "notes": notes[user.pk]}
    return sorted(rows.values(), key=lambda row: (-(row["moves"] + row["notes"]), row["name"]))


# --- candidate ------------------------------------------------------------


def candidate_insights(profile: CandidateProfile, *, days: int = 90) -> dict[str, Any]:
    """The candidate's own numbers (§16.2).

    Computed live and deliberately: §5.14 states there is no per-candidate daily
    rollup, because one person's data is small and a nightly job per candidate
    would produce a table larger than what it summarises.
    """
    from apps.applications.models import Application, StageKind
    from apps.candidates.models import ProfileView
    from apps.resumes.models import ResumeAnalysis

    since = timezone.now() - dt.timedelta(days=days)

    applications = Application.objects.filter(candidate=profile, applied_at__gte=since)
    by_outcome = applications.aggregate(
        total=Count("id"),
        interviewing=Count("id", filter=Q(stage__kind=StageKind.INTERVIEW)),
        offered=Count("id", filter=Q(offers__isnull=False), distinct=True),
        closed=Count("id", filter=Q(closed_at__isnull=False)),
    )

    scores = list(
        ResumeAnalysis.objects.filter(version__document__profile=profile, created_at__gte=since)
        .order_by("created_at")
        .values_list("created_at", "score", "algorithm_version")
    )

    return {
        "days": days,
        "applications": by_outcome["total"] or 0,
        "interviewing": by_outcome["interviewing"] or 0,
        "offers": by_outcome["offered"] or 0,
        "closed": by_outcome["closed"] or 0,
        "interview_rate": _rate(by_outcome["interviewing"], by_outcome["total"]),
        "profile_views": ProfileView.objects.filter(
            candidate=profile, created_at__gte=since
        ).count(),
        "score_trend": [
            {"at": at, "score": score, "comparable": version > 0} for at, score, version in scores
        ],
        "employer_response": _candidate_response_view(profile, since=since),
        "has_data": (by_outcome["total"] or 0) > 0,
    }


# --- platform -------------------------------------------------------------


def ai_cost(*, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """What AI cost and how well it worked (§16.2, platform view)."""
    since = timezone.localdate() - dt.timedelta(days=days)
    rows = list(AICostDaily.objects.filter(day__gte=since))

    by_task: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_task.setdefault(
            row.task_key,
            {
                "task": row.task_key,
                "runs": 0,
                "failures": 0,
                "cached": 0,
                "cost": Decimal("0"),
                "latency": 0,
            },
        )
        entry["runs"] += row.runs
        entry["failures"] += row.failures
        entry["cached"] += row.cached
        entry["cost"] += row.cost
        entry["latency"] += row.latency_ms_total

    tasks = sorted(by_task.values(), key=lambda item: item["cost"], reverse=True)
    for entry in tasks:
        entry["failure_rate"] = _rate(entry["failures"], entry["runs"])
        entry["cache_rate"] = _rate(entry["cached"], entry["runs"])
        entry["average_latency_ms"] = (
            round(entry["latency"] / entry["runs"]) if entry["runs"] else 0
        )

    return {
        "days": days,
        "tasks": tasks,
        "total_cost": sum((entry["cost"] for entry in tasks), Decimal("0")),
        "total_runs": sum(entry["runs"] for entry in tasks),
        "has_data": bool(tasks),
    }


# --- internals ------------------------------------------------------------


def _rate(part: int | None, whole: int | None) -> float:
    return round((part or 0) / whole, 4) if whole else 0.0


def _latest_day(model: Any, **filters: Any) -> dt.date | None:
    row = model.objects.filter(**filters).order_by("-day").values_list("day", flat=True).first()
    return row


def _offer_acceptance(company: Company, *, since: dt.date) -> float:
    from apps.applications.models import Offer, OfferState

    offers = Offer.objects.filter(application__job__company=company, created_at__date__gte=since)
    decided = offers.filter(state__in=[OfferState.ACCEPTED, OfferState.DECLINED]).count()
    accepted = offers.filter(state=OfferState.ACCEPTED).count()
    return _rate(accepted, decided)


def _response_nudge(responded: int, applications: int, hours: float) -> str:
    """One sentence, or none (§16.3.3).

    A rate rather than a scolding. An employer replying to two in three is doing
    better than most of the market, and telling them they are failing is how the
    nudge gets ignored and then switched off.
    """
    if not applications:
        return ""
    rate = responded / applications
    average = hours / responded if responded else 0

    if rate < 0.5:
        return (
            f"You have replied to {responded} of {applications} applications. "
            "Candidates rank never hearing back as the worst part of applying anywhere."
        )
    if average > 24 * 14:
        return (
            f"Replies take about {round(average / 24)} days on average. "
            "Most candidates have accepted something else by then."
        )
    return ""


def _candidate_response_view(profile: CandidateProfile, *, since: dt.datetime) -> dict[str, Any]:
    """How often the companies this candidate applied to actually replied.

    The same measurement as §16.3.3, from the other side. Shown to the candidate
    because "you are not being ignored personally, this employer replies to one
    in five" is a genuinely useful thing to know while job hunting.
    """
    from apps.applications.models import Application

    applications = Application.objects.filter(candidate=profile, applied_at__gte=since)
    total = applications.count()
    if not total:
        return {"replied": 0, "total": 0, "rate": 0.0}

    replied = applications.exclude(stage__key="new").count()
    return {"replied": replied, "total": total, "rate": _rate(replied, total)}
