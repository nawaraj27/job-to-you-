"""
Talent search: who may be found, who may be read, and what is recorded
(FR-9.1 to FR-9.4, SRS §10.6, INV-4).

**This module is the authorization boundary.** `candidates.search` ranks a
queryset; it never decides what is in one. Everything about who an employer may
see is here, in one place, because a rule with two homes is a rule that drifts.

Three things it is answerable for:

  * **Searchable is not the same as readable.** A candidate whose visibility is
    `everyone` appears in results — that setting *is* their opt-in to being
    found. Opening their profile is a further act that creates a
    `CandidateVisibilityGrant` on the `public` basis and writes a `ProfileView`,
    so INV-4 holds: every employer read of a candidate is authorised by a grant
    row, including the first one.
  * **Every read is visible to the person read** (FR-2.8, FR-9.3). The
    `ProfileView` is written in the same transaction as the grant. An employer
    cannot look without the candidate being able to see that they looked.
  * **Export is a different act from search** (FR-9.4). It needs
    `candidate.export` rather than `candidate.search`, it is capped, every row
    exported is a logged view, and it feeds anomaly detection — because a bulk
    export is what a scraper holding a paid employer account does on its way
    out.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.urls import reverse

from apps.applications.models import CandidateVisibilityGrant, GrantBasis
from apps.applications.services import visibility as visibility_service
from apps.audit import services as audit
from apps.common import ratelimit
from apps.common.errors import InvalidState, NotFound
from apps.companies.permissions import Grant, require

from ..models import BlockedCompany, CandidateProfile, ProfileView, ViewSource, Visibility
from ..search import TalentQuery, TalentResult, is_known_short_skill
from ..search import search as run_search

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company, CompanyMembership

logger = logging.getLogger(__name__)

#: How many rows one export may contain. Not a performance limit — a deliberate
#: friction. An employer with a real shortlist is well under it; a scraper wants
#: to be well over it, and hitting the cap is itself a signal.
EXPORT_LIMIT = 100

#: What an export may contain. An allow-list rather than "everything on the
#: model", so a column added later — a phone number, a date of birth — cannot
#: leave the platform in a spreadsheet because nobody thought about it.
EXPORT_FIELDS: tuple[str, ...] = (
    "public_id",
    "headline",
    "location",
    "experience_months",
    "seeking",
    "skills",
    "profile_url",
)


def searchable_for(
    company: Company, membership: CompanyMembership | None = None
) -> QuerySet[CandidateProfile]:
    """Every candidate this company may find (FR-9.1).

    Two ways in, and they are different consents:

      * `visibility = everyone` — the candidate opted in to being discoverable.
      * a live grant — usually because they applied here, which is consent to
        this company specifically and to nobody else.

    A candidate on `employers` who has not applied is **not** discoverable:
    "employers you've applied to" is a narrower statement than "employers", and
    reading it as the wider one would be the platform making the candidate's
    decision for them.

    With a `membership`, a grant that exists only because somebody applied
    counts only when that member's read reaches the application (decision
    D-13): a recruiter must not find, through search, the applicants to a job
    they are not assigned to.
    """
    grants = CandidateVisibilityGrant.objects.live().filter(company=company)
    if membership is not None:
        from apps.applications import policies as application_policies
        from apps.applications.models import Application

        if application_policies.read_scope(membership) != application_policies.SCOPE_COMPANY:
            grants = grants.filter(
                ~Q(basis=GrantBasis.APPLICATION)
                | Q(application__in=Application.objects.visible_to(membership).values("pk"))
            )
    granted = grants.values("candidate_id")
    # A block is a boundary, not a filter on grants. Blocking revoked this
    # company's grants, but a public profile needs no grant to be found — so a
    # blocked company still found the candidate, opened the profile, and got a
    # fresh `public` grant for doing it.
    blocked = BlockedCompany.objects.filter(company=company).values("profile_id")

    return (
        CandidateProfile.objects.searchable()
        .filter(Q(visibility=Visibility.EVERYONE) | Q(pk__in=granted))
        .exclude(pk__in=blocked)
        .distinct()
    )


def search(*, membership: CompanyMembership, actor: User, query: TalentQuery) -> TalentResult:
    """Run one search for one company (FR-9.1, FR-9.2).

    Requires `candidate.search`. An empty query is refused rather than answered
    with the corpus: "show me everyone" is not a search, and it is the shape a
    scraper's first request takes.

    **Searching logs nothing per candidate.** A result row is a headline and a
    skill list, not a profile read, and writing a view for every row of every
    search would drown the candidate's "who viewed me" screen in noise until it
    told them nothing. The read is logged when somebody opens the profile.
    """
    require(membership, Grant.CANDIDATE_SEARCH)

    if query.is_empty:
        raise InvalidState("Add a skill, a place or some words to search for.")

    # Per membership, not per company and not per IP (§15.4, deferred out of
    # P15 to the hardening pass). Per company would let one compromised seat
    # spend the whole team's allowance; per IP would punish an office. Thirty a
    # minute is far above real use and far below a script's.
    ratelimit.check(ratelimit.CANDIDATE_SEARCH_PER_MEMBERSHIP, f"membership:{membership.pk}")

    # Decision D-05. One letter matches by substring almost everyone; it is
    # searched only when it is a skill's whole name or alias (R, C). Two
    # letters match skills exactly (Go, UI) and are never refused.
    if len(query.text) == 1 and not is_known_short_skill(query.text):
        raise InvalidState(
            f'"{query.text}" is too short to search for. Type at least three letters, '
            "or a skill's full name such as R or C."
        )

    result = run_search(query, base=searchable_for(membership.company, membership))

    audit.record(
        verb="candidate.searched",
        actor=actor,
        target=membership.company,
        company=membership.company,
        metadata={
            # What was searched for, never who came back. The result set is a
            # list of people, and an audit row is not the place to keep one.
            "skills": len(query.skill_ids),
            "has_text": bool(query.text),
            "results": result.total,
        },
    )
    return result


@transaction.atomic
def view_profile(
    *,
    membership: CompanyMembership,
    actor: User,
    public_id: str,
    source: str = ViewSource.SEARCH,
) -> CandidateProfile:
    """Open one candidate's profile (FR-9.1, FR-9.3, INV-4).

    Creates the grant if the candidate's own visibility setting permits it, and
    records the view either way. Both happen in one transaction: a read that was
    authorised but not recorded is exactly the read a candidate is entitled to
    know about.

    `NotFound` for a candidate this company may not see — never `NotAllowed`,
    because a 403 would confirm that a person with that id exists and is on the
    platform, which is the fact the visibility setting exists to withhold.
    """
    require(membership, Grant.CANDIDATE_SEARCH)

    profile = (
        searchable_for(membership.company, membership)
        .select_related("user", "location")
        .filter(public_id=public_id)
        .first()
    )
    if profile is None:
        raise NotFound("That candidate is not available.")

    if profile.visibility == Visibility.EVERYONE:
        # Their opt-in, made concrete. The grant is what INV-4 requires every
        # later read to point at, and it is created here rather than at search
        # time because appearing in a list is not being read.
        visibility_service.grant(
            candidate=profile, company=membership.company, basis=GrantBasis.PUBLIC
        )

    record_view(profile=profile, membership=membership, actor=actor, source=source)
    return profile


def record_view(
    *,
    profile: CandidateProfile,
    membership: CompanyMembership,
    actor: User,
    source: str = ViewSource.SEARCH,
) -> ProfileView:
    """Write the view the candidate will see (FR-2.8).

    A row per read rather than a counter, because "who and when" is the question
    and a counter cannot answer it. Deduplication is a reading concern: the
    candidate's screen groups by company and day, so an employer who opens a
    profile four times in an afternoon appears once.
    """
    view = ProfileView.objects.create(
        candidate=profile,
        company=membership.company,
        actor=actor,
        source=source if source in ViewSource.values else ViewSource.SEARCH,
    )
    audit.record(
        verb="candidate.viewed",
        actor=actor,
        target=profile,
        company=membership.company,
        metadata={"source": view.source},
    )

    from . import anomaly

    # Checked here rather than in a nightly job alone, because the rate that
    # matters is measured in hours: a scraper that reads the corpus overnight
    # has finished by the time a morning sweep notices.
    anomaly.check_viewing(company=membership.company, actor=actor)
    return view


@transaction.atomic
def export(
    *, membership: CompanyMembership, actor: User, public_ids: list[str]
) -> list[dict[str, Any]]:
    """Export a chosen shortlist (FR-9.4).

    Requires `candidate.export`, which no role holds by default below admin —
    taking a list of people off the platform is a different act from looking at
    them one at a time, and it is the act worth making somebody grant
    explicitly.

    **Every exported row is a logged view.** An export that did not appear on
    the candidate's "who viewed me" screen would be a way to read a hundred
    profiles invisibly, which is precisely the thing FR-9.3 exists to prevent.
    """
    require(membership, Grant.CANDIDATE_EXPORT)

    # The cap is measured on the request as sent: an oversized request is the
    # signal, whatever it turns out to contain.
    if len(public_ids) > EXPORT_LIMIT:
        raise InvalidState(
            f"An export may hold {EXPORT_LIMIT} candidates at a time. "
            "Narrow the search and export in batches."
        )
    # A tampered or truncated form value reached the UUID filter and was a 500.
    # Duplicates are dropped so nobody is logged as viewed twice by one export.
    public_ids = list(dict.fromkeys(pid for pid in public_ids if _is_public_id(pid)))
    if not public_ids:
        raise InvalidState("Choose some candidates to export.")

    profiles = list(
        searchable_for(membership.company, membership)
        .select_related("location")
        .prefetch_related("skills__skill")
        .filter(public_id__in=public_ids)
    )

    rows = []
    for profile in profiles:
        record_view(profile=profile, membership=membership, actor=actor, source=ViewSource.SEARCH)
        rows.append(_export_row(profile, company_slug=membership.company.slug))

    audit.record(
        verb="candidate.exported",
        actor=actor,
        target=membership.company,
        company=membership.company,
        metadata={"count": len(rows), "requested": len(public_ids)},
    )

    from . import anomaly

    anomaly.check_export(company=membership.company, actor=actor, count=len(rows))
    return rows


def views_for(profile: CandidateProfile, *, limit: int = 50) -> list[dict[str, Any]]:
    """ "Who viewed your profile", grouped (FR-2.8).

    Grouped by company and day so an employer who opened a profile four times in
    an afternoon appears once. Ungrouped, an interested employer looks like
    surveillance, and the screen stops being reassuring and starts being
    alarming for no reason.

    The individual who looked is **not** named. The company is accountable for
    the read; naming the recruiter would expose one person's browsing to another
    without their knowledge, and it is not what the candidate is asking.
    """
    from django.contrib.postgres.aggregates import ArrayAgg
    from django.db.models import Count, Max
    from django.db.models.functions import TruncDate

    rows = (
        ProfileView.objects.filter(candidate=profile)
        .annotate(day=TruncDate("created_at"))
        # Not grouped by source: a company that opened the profile from an
        # application and again from a search appeared twice for the same day,
        # which is the "reads as surveillance" effect the grouping exists to stop.
        .values("company__name", "company__slug", "day")
        .annotate(
            times=Count("id"),
            last_seen=Max("created_at"),
            sources=ArrayAgg("source", distinct=True),
        )
        .order_by("-day", "company__name")[:limit]
    )
    return [
        {
            "company": row["company__name"],
            "slug": row["company__slug"],
            "day": row["day"],
            "times": row["times"],
            "last_seen": row["last_seen"],
            "sources": sorted(row["sources"], key=_SOURCE_ORDER.index),
            "source": sorted(row["sources"], key=_SOURCE_ORDER.index)[0],
        }
        for row in rows
    ]


# --- internals ------------------------------------------------------------


#: How a view is described, most specific first.
_SOURCE_ORDER = [ViewSource.APPLICATION, ViewSource.POOL, ViewSource.SEARCH]


def _is_public_id(value: Any) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _export_row(profile: CandidateProfile, *, company_slug: str) -> dict[str, Any]:
    """One exported candidate, restricted to `EXPORT_FIELDS`.

    No email, no phone, no name. An export is a list of leads to follow up on
    the platform, not a contact list to take off it — contact details are
    revealed by the staged rules in ADR-A34, at a stage that requires an
    application, and an export is not that stage.
    """
    return {
        "public_id": str(profile.public_id),
        "headline": profile.headline,
        "location": profile.location.name if profile.location is not None else "",
        "experience_months": profile.experience_months,
        "seeking": profile.get_seeking_display() if profile.seeking else "",
        "skills": ", ".join(row.skill.name for row in profile.skills.all()[:20]),
        # Was `/employer/talent/<id>/`, a path with no company in it that 404s.
        "profile_url": reverse("employer:talent_profile", args=[company_slug, profile.public_id]),
    }
