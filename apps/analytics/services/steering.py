"""
The four measurements that steer the product (SRS §16.3).

These are not dashboard numbers. They are the four questions whose answers
decide whether the product's central claims are true, and each one is designed
to be able to come back **no**:

1. **Does the match score predict interviews and hires?** If not, §10.2's
   weights are wrong and the scorer changes. Instrumented from P13.
2. **Are AI suggestions accepted or ignored?** The best single measure of
   whether the AI is useful or noise.
3. **Do employers respond, and how fast?** Becomes a nudge, then a badge.
4. **Does any interview format predict a hire?** Whether §11 predicts anything.

Every one carries its `sample_size`, and `is_meaningful` is false below thirty.
A correlation over eleven applications is not a finding, and a number shown
without its sample invites somebody to act on eleven applications.

**Nothing here changes behaviour.** These write snapshots a person reads. A
metric that automatically retuned the scorer would be a system marking its own
homework, and the whole point of measuring is that somebody can be told the
answer is no.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from django.db.models import Q, Sum
from django.utils import timezone

from ..models import SteeringMetric, SteeringSnapshot

logger = logging.getLogger(__name__)

#: See `rollups.HIRED_STAGE_KEY`. Imported rather than redefined so the two
#: cannot drift into disagreeing about what a hire is.
from .rollups import HIRED_STAGE_KEY  # noqa: E402

#: Below this, a number is noise wearing a decimal point.
MEANINGFUL_SAMPLE = 30

#: How far back each measurement looks. Ninety days: long enough for an
#: application to reach an outcome, short enough that a change to the scorer
#: shows up within a quarter.
WINDOW_DAYS = 90


def measure_all(*, day: dt.date | None = None) -> list[SteeringSnapshot]:
    """Take all four measurements. What the nightly task calls."""
    day = day or timezone.localdate()
    return [
        measure_match_predicts_outcome(day=day),
        measure_ai_suggestions_accepted(day=day),
        measure_employer_response(day=day),
        measure_interview_predicts_hire(day=day),
    ]


def measure_match_predicts_outcome(*, day: dt.date | None = None) -> SteeringSnapshot:
    """Does a higher match score mean a better outcome? (§16.3.1)

    Measured as the difference between the mean frozen match score of
    applications that reached an interview and those that did not. Positive
    means the scorer is telling the truth; near zero means the weights are
    sorting noise, and §10.2 changes.

    The **frozen** snapshot is used, not a recomputed score (FR-8.5). Comparing
    today's scorer against last quarter's outcomes would measure the scorer
    against itself.
    """
    from apps.applications.models import Application, StageKind

    day = day or timezone.localdate()
    since = timezone.now() - dt.timedelta(days=WINDOW_DAYS)

    scored = Application.objects.filter(applied_at__gte=since, match_snapshot__score__isnull=False)
    # "Advanced" is an interview, an offer, or a hire. By kind for the first
    # two and by key for the last, because `hired` and `declined` share the
    # `terminal` kind — counting by kind would put every rejection in the
    # advanced group and make a working scorer look broken.
    advanced_filter = Q(stage__kind__in=[StageKind.INTERVIEW, StageKind.OFFER]) | Q(
        stage__key=HIRED_STAGE_KEY
    )
    advanced = _mean_score(scored.filter(advanced_filter))
    rest = _mean_score(scored.exclude(advanced_filter))
    sample = scored.count()

    return _write(
        metric=SteeringMetric.MATCH_PREDICTS_OUTCOME,
        day=day,
        value=Decimal(f"{advanced - rest:.4f}"),
        sample_size=sample,
        detail={
            "mean_score_advanced": round(advanced, 2),
            "mean_score_not_advanced": round(rest, 2),
            "window_days": WINDOW_DAYS,
            # Stated so nobody reads a gap of 0.4 as vindication.
            "reads_as": _reads_as(advanced - rest),
        },
    )


def measure_ai_suggestions_accepted(*, day: dt.date | None = None) -> SteeringSnapshot:
    """Are AI suggestions used, or ignored? (§16.3.2)

    Counted over `ResumeSuggestion`, which is the one table every AI proposal
    passes through on its way to a document (INV-3). That is what makes this
    measurable at all: because nothing can reach a CV without becoming a row
    here first, the acceptance rate is a fact rather than an estimate.
    """
    from apps.resumes.models import ResumeSuggestion, SuggestionState

    day = day or timezone.localdate()
    since = timezone.now() - dt.timedelta(days=WINDOW_DAYS)

    offered = ResumeSuggestion.objects.filter(created_at__gte=since).exclude(source="reconcile")
    decided = offered.exclude(state=SuggestionState.OFFERED)
    accepted = offered.filter(state=SuggestionState.ACCEPTED).count()
    total = decided.count()

    return _write(
        metric=SteeringMetric.AI_SUGGESTIONS_ACCEPTED,
        day=day,
        value=Decimal(f"{(accepted / total) if total else 0:.4f}"),
        sample_size=total,
        detail={
            "accepted": accepted,
            "decided": total,
            "still_open": offered.filter(state=SuggestionState.OFFERED).count(),
            "window_days": WINDOW_DAYS,
        },
    )


def measure_employer_response(*, day: dt.date | None = None) -> SteeringSnapshot:
    """Do employers reply, and how quickly? (§16.3.3)

    Read from `CompanyDailyStat` rather than recomputed, because the rollup
    already answers it and two implementations of "did they respond" would
    disagree about the same company within a month.
    """
    from ..models import CompanyDailyStat

    day = day or timezone.localdate()
    since = day - dt.timedelta(days=WINDOW_DAYS)

    rows = CompanyDailyStat.objects.filter(day__gte=since, day__lte=day).aggregate(
        total_applications=Sum("applications"),
        total_responded=Sum("responded"),
        total_hours=Sum("response_hours_total"),
    )
    applications = rows["total_applications"] or 0
    responded = rows["total_responded"] or 0
    hours = float(rows["total_hours"] or 0)

    return _write(
        metric=SteeringMetric.EMPLOYER_RESPONSE,
        day=day,
        value=Decimal(f"{(responded / applications) if applications else 0:.4f}"),
        sample_size=applications,
        detail={
            "responded": responded,
            "applications": applications,
            "average_hours": round(hours / responded, 1) if responded else 0,
            "window_days": WINDOW_DAYS,
        },
    )


def measure_interview_predicts_hire(*, day: dt.date | None = None) -> SteeringSnapshot:
    """Does an interview recommendation predict a hire? (§16.3.4)

    The share of positively-recommended interviews whose application reached
    `hired`, against the share of negatively-recommended ones that did. If those
    two numbers are close, the interview format is not predicting anything and
    §11 needs rethinking — which is a conclusion this measurement is designed to
    be able to reach.
    """
    from apps.interviews.models import InterviewOutcome, Recommendation

    day = day or timezone.localdate()
    since = timezone.now() - dt.timedelta(days=WINDOW_DAYS)

    outcomes = InterviewOutcome.objects.filter(
        created_at__gte=since, session__application__isnull=False
    ).select_related("session__application__stage")

    positive = outcomes.filter(recommendation__in=[Recommendation.STRONG_YES, Recommendation.YES])
    negative = outcomes.exclude(recommendation__in=[Recommendation.STRONG_YES, Recommendation.YES])

    positive_hired = _hire_rate(positive)
    negative_hired = _hire_rate(negative)
    sample = outcomes.count()

    return _write(
        metric=SteeringMetric.INTERVIEW_PREDICTS_HIRE,
        day=day,
        value=Decimal(f"{positive_hired - negative_hired:.4f}"),
        sample_size=sample,
        detail={
            "hire_rate_after_yes": round(positive_hired, 4),
            "hire_rate_after_no": round(negative_hired, 4),
            "window_days": WINDOW_DAYS,
            "reads_as": _reads_as(positive_hired - negative_hired, scale=0.1),
        },
    )


def latest() -> dict[str, SteeringSnapshot]:
    """The most recent value of each measurement, for the staff screen."""
    found: dict[str, SteeringSnapshot] = {}
    for metric in SteeringMetric.values:
        row = SteeringSnapshot.objects.filter(metric=metric).order_by("-day").first()
        if row is not None:
            found[metric] = row
    return found


def history(metric: str, *, days: int = 180) -> list[SteeringSnapshot]:
    """One measurement over time. A chart of whether things got better."""
    since = timezone.localdate() - dt.timedelta(days=days)
    return list(SteeringSnapshot.objects.filter(metric=metric, day__gte=since).order_by("day"))


# --- internals ------------------------------------------------------------


def _write(
    *, metric: str, day: dt.date, value: Decimal, sample_size: int, detail: dict[str, Any]
) -> SteeringSnapshot:
    """Idempotent per (day, metric), like every other rollup."""
    snapshot, _ = SteeringSnapshot.objects.update_or_create(
        metric=metric,
        day=day,
        defaults={
            "value": value,
            "sample_size": sample_size,
            "detail": detail,
            "computed_at": timezone.now(),
        },
    )
    return snapshot


def _mean_score(queryset: Any) -> float:
    """Mean frozen match score over a queryset of applications.

    In Python because `match_snapshot__score` is a JSON key: Postgres can
    average it with a cast, and the cast makes the query unreadable for a number
    computed once a night over a bounded window.
    """
    scores = [
        row.get("score", 0)
        for row in queryset.values_list("match_snapshot", flat=True)
        if isinstance(row, dict) and isinstance(row.get("score"), int)
    ]
    return sum(scores) / len(scores) if scores else 0.0


def _hire_rate(queryset: Any) -> float:
    total = queryset.count()
    if not total:
        return 0.0
    hired = queryset.filter(session__application__stage__key=HIRED_STAGE_KEY).count()
    return hired / total


def _reads_as(difference: float, *, scale: float = 5.0) -> str:
    """What the number means, in words, so nobody has to guess.

    Written here rather than in a template because the thresholds are a
    judgement about the measurement, and a judgement in a template is a
    judgement nobody reviews.
    """
    if difference >= scale:
        return "predictive"
    if difference >= scale / 2:
        return "weakly predictive"
    if difference > -scale / 2:
        return "not predictive — the weights are sorting noise"
    return "inverted — higher scores are doing worse, which is a bug"
