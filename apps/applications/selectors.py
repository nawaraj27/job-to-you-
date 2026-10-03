"""
Application reads (DEVELOPER.md §1.2).

The scoping is in the query, never in a check afterwards. Two boundaries:
`for_company` for anything an employer sees, `for_candidate` for anything a
candidate sees — and the timeline splits into two filtered views of one stream
rather than into two tables (FR-6.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Avg, Count, F, Prefetch, Q, QuerySet

from apps.common.ids import as_uuid

from .models import (
    Application,
    ApplicationEvent,
    ApplicationNote,
    ApplicationQuerySet,
    ApplicationRating,
    CandidateVisibilityGrant,
    Offer,
    OfferState,
    PipelineStage,
    StageKind,
    TalentPool,
    TalentPoolMember,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company, CompanyMembership
    from apps.jobs.models import Job


# --- employer -------------------------------------------------------------


def applications_for(membership: CompanyMembership) -> ApplicationQuerySet:
    """Everything this team member may read (SRS §2.3).

    Returns the custom queryset rather than a bare `QuerySet[Application]` so a
    caller can keep chaining `live()` without losing it.
    """
    return (
        Application.objects.visible_to(membership)
        .select_related("job", "candidate", "candidate__user", "stage")
        .annotate(rating_average=Avg("ratings__score"))
    )


def application_for(membership: CompanyMembership, public_id: str) -> Application | None:
    if as_uuid(public_id) is None:
        return None
    return (
        Application.objects.visible_to(membership)
        .select_related(
            "job",
            "job__company",
            "candidate",
            "candidate__user",
            "stage",
            "resume_version",
            "cover_letter",
        )
        .prefetch_related("tag_links__tag", "offers")
        .filter(public_id=public_id)
        .first()
    )


def pipeline_board(
    membership: CompanyMembership, *, job: Job | None = None, order: str = "recent"
) -> list[dict]:
    """The applicant pipeline, grouped by stage.

    One query for the counts and one for the rows, rather than one per column:
    a seven-stage board is otherwise fourteen queries to draw a page.

    `order="match"` sorts each column by the score frozen on the application at
    submission (FR-8.4, FR-8.5). Read, never recomputed — an employer comparing
    two candidates must be comparing the numbers that existed when each applied,
    not two scores from two versions of two profiles.
    """
    from .services import pipeline as pipeline_service

    queryset = applications_for(membership).live()
    if job is not None:
        queryset = queryset.filter(job=job)

    if order == "match":
        # Nulls last: an application whose snapshot failed must not sort above a
        # scored one, and must not disappear either.
        queryset = queryset.order_by(
            "stage__order", F("match_snapshot__score").desc(nulls_last=True), "-applied_at"
        )
        rows = list(queryset)
    else:
        rows = list(queryset.order_by("stage__order", "-applied_at"))
    by_stage: dict[int, list[Application]] = {}
    for application in rows:
        by_stage.setdefault(application.stage_id, []).append(application)

    return [
        {
            "stage": stage,
            "applications": by_stage.get(stage.pk, []),
            "count": len(by_stage.get(stage.pk, [])),
        }
        for stage in pipeline_service.stages_for(membership.company)
    ]


def counts_for(membership: CompanyMembership) -> dict[str, int]:
    """The employer dashboard's numbers, in one query."""
    rows = Application.objects.visible_to(membership).aggregate(
        live=Count("id", filter=Q(withdrawn_at__isnull=True, closed_at__isnull=True)),
        new=Count("id", filter=Q(stage__key="new", withdrawn_at__isnull=True)),
        interviewing=Count("id", filter=Q(stage__kind=StageKind.INTERVIEW)),
        closed=Count("id", filter=Q(closed_at__isnull=False)),
    )
    return {key: value or 0 for key, value in rows.items()}


def notes_for(application: Application) -> QuerySet[ApplicationNote]:
    """Company-internal, permanently (FR-6.11)."""
    return application.notes.filter(deleted_at__isnull=True).select_related("author")


def ratings_for(application: Application) -> QuerySet[ApplicationRating]:
    return application.ratings.select_related("rater")


def employer_timeline(application: Application) -> QuerySet[ApplicationEvent]:
    """Everything that happened. The unfiltered stream."""
    return application.timeline.select_related("actor", "from_stage", "to_stage")


def prior_applications(
    membership: CompanyMembership, application: Application
) -> QuerySet[Application]:
    """Candidate rediscovery (FR-6.12).

    "We have spoken to this person before" is the single most useful thing a
    recruiter can know when opening an application, and it is one query.
    """
    return (
        Application.objects.visible_to(membership)
        .filter(candidate_id=application.candidate_id)
        .exclude(pk=application.pk)
        .select_related("job", "stage")
        .order_by("-applied_at")
    )


def live_offer(application: Application) -> Offer | None:
    return application.offers.filter(state__in=[OfferState.DRAFT, OfferState.SENT]).first()


def offers_for(application: Application) -> QuerySet[Offer]:
    """Every offer, including the superseded ones — the history is the point."""
    return application.offers.select_related("sent_by")


def stages_for(company: Company) -> QuerySet[PipelineStage]:
    return PipelineStage.objects.for_company(company)


def pools_for(company: Company) -> QuerySet[TalentPool]:
    return TalentPool.objects.filter(company=company).annotate(
        member_count=Count("members", filter=Q(members__deleted_at__isnull=True))
    )


def pool_members(pool: TalentPool) -> QuerySet[TalentPoolMember]:
    return pool.members.filter(deleted_at__isnull=True).select_related(
        "candidate", "candidate__user"
    )


# --- candidate ------------------------------------------------------------


def applications_of(profile: CandidateProfile) -> QuerySet[Application]:
    return (
        Application.objects.for_candidate(profile)
        .select_related("job", "job__company", "stage")
        .prefetch_related(Prefetch("offers", queryset=Offer.objects.order_by("-created_at")))
    )


def application_of(profile: CandidateProfile, public_id: str) -> Application | None:
    if as_uuid(public_id) is None:
        return None
    return (
        Application.objects.for_candidate(profile)
        .select_related("job", "job__company", "stage", "resume_version", "cover_letter")
        .filter(public_id=public_id)
        .first()
    )


def candidate_timeline(application: Application) -> QuerySet[ApplicationEvent]:
    """The same stream, filtered (FR-6.4).

    Not a second table. Two streams drift, and they drift in the direction of
    the candidate being told less than what happened.
    """
    return application.timeline.filter(visible_to_candidate=True).select_related(
        "from_stage", "to_stage"
    )


def grants_for(profile: CandidateProfile) -> QuerySet[CandidateVisibilityGrant]:
    """Who can see me, and why (INV-4). The candidate's own answer."""
    return (
        CandidateVisibilityGrant.objects.live()
        .filter(candidate=profile)
        .select_related("company", "application", "application__job")
    )


def cover_letters_for(profile: CandidateProfile) -> QuerySet:
    return profile.cover_letters.select_related("job")
