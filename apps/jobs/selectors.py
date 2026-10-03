"""
Job reads (DEVELOPER.md §1.2).

Two scoping rules, applied in the query rather than checked afterwards:

  * employer reads start from `for_company(company)` — the tenant boundary
    (ADR-A18);
  * public reads start from `published()`, which also excludes suspended
    companies, because a suspension that leaves jobs reachable is not one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Count, Prefetch, Q, QuerySet

from apps.common.ids import as_uuid

from .models import Job, JobAlertMatch, JobQuerySet, JobSkill, JobStatus

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile, SavedSearch
    from apps.companies.models import Company, CompanyMembership


def jobs_for_company(company: Company) -> JobQuerySet:
    """Every posting a company owns, with the skill count the list shows.

    Returns the custom queryset rather than a bare `QuerySet[Job]` so a caller
    can keep chaining `for_company` / `published` without losing them.
    """
    return (
        Job.objects.for_company(company)
        .select_related("location", "category", "created_by")
        .annotate(skill_count=Count("skills", distinct=True))
    )


def job_for_company(company: Company, public_id: str) -> Job | None:
    """One posting, scoped to its company in the query.

    Scoped here rather than checked afterwards: a lookup by public id alone
    would return another tenant's row and leave the boundary to whoever
    remembers to test for it.
    """
    if as_uuid(public_id) is None:
        return None
    return (
        Job.objects.for_company(company)
        .select_related("company", "location", "category")
        .prefetch_related(
            Prefetch("skills", queryset=JobSkill.objects.select_related("skill")),
            "questions",
            "assignments__membership__user",
        )
        .filter(public_id=public_id)
        .first()
    )


def board_for_company(company: Company) -> QuerySet[Job]:
    """The company's public page: what a candidate sees of them."""
    return Job.objects.published().filter(company=company).select_related("location", "category")


def counts_for_company(company: Company) -> dict[str, int]:
    """The dashboard's numbers, in one query rather than five."""
    rows = Job.objects.for_company(company).aggregate(
        drafts=Count("id", filter=Q(status=JobStatus.DRAFT)),
        published=Count("id", filter=Q(status=JobStatus.PUBLISHED)),
        paused=Count("id", filter=Q(status=JobStatus.PAUSED)),
        closed=Count("id", filter=Q(status__in=[JobStatus.CLOSED, JobStatus.EXPIRED])),
        archived=Count("id", filter=Q(status=JobStatus.ARCHIVED)),
    )
    return {key: value or 0 for key, value in rows.items()}


def assigned_to(membership: CompanyMembership) -> QuerySet[Job]:
    """The postings one team member is responsible for."""
    return (
        Job.objects.for_company(membership.company)
        .filter(assignments__membership=membership)
        .distinct()
    )


# --- public ---------------------------------------------------------------


def published_job(slug: str) -> Job | None:
    return (
        Job.objects.published()
        .select_related("company", "location", "category")
        .prefetch_related(Prefetch("skills", queryset=JobSkill.objects.select_related("skill")))
        .filter(slug=slug)
        .first()
    )


def similar_to(job: Job, *, limit: int = 4) -> QuerySet[Job]:
    """Other open roles someone reading this one might want.

    Same category or same company, never the job itself. Deliberately not a
    match score: that is P13's, and a "similar" list that pretended to be
    ranked would be read as one.
    """
    return (
        Job.objects.published()
        .filter(Q(category_id=job.category_id) | Q(company_id=job.company_id))
        .exclude(pk=job.pk)
        .select_related("company", "location")[:limit]
    )


# --- candidate ------------------------------------------------------------


def saved_jobs_for(profile: CandidateProfile) -> QuerySet:
    return profile.saved_jobs.select_related("job", "job__company", "job__location")


def saved_searches_for(profile: CandidateProfile) -> QuerySet:
    return profile.saved_searches.all()


def saved_search_for(profile: CandidateProfile, pk: int) -> SavedSearch | None:
    """Scoped to the owner, so a guessed id resolves to nothing."""
    return profile.saved_searches.filter(pk=pk).first()


def saved_job_ids(profile: CandidateProfile | None) -> set[int]:
    """Which of the jobs on screen are already saved.

    One query for the whole page rather than one per row — a list of twenty
    jobs should not be twenty extra queries to draw a bookmark icon.
    """
    if profile is None:
        return set()
    return set(profile.saved_jobs.values_list("job_id", flat=True))


def saved_job_slugs(profile: CandidateProfile | None) -> set[str]:
    """The same answer keyed by slug, which is what the templates hold."""
    if profile is None:
        return set()
    return set(profile.saved_jobs.values_list("job__slug", flat=True))


def alert_matches_for(profile: CandidateProfile, *, limit: int = 50) -> QuerySet[JobAlertMatch]:
    return (
        JobAlertMatch.objects.filter(saved_search__profile=profile)
        .select_related("job", "job__company", "saved_search")
        .order_by("-computed_at")[:limit]
    )
