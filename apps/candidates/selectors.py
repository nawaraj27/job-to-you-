"""
Candidate reads (DEVELOPER.md §1.2).

Scoping here is by **owner**, not by tenant: a candidate profile belongs to one
user, and the employer-side read is gated by visibility rather than membership.
Every function that returns someone else's profile takes the viewer explicitly,
so a caller cannot forget to pass one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Prefetch, QuerySet

from apps.common.ids import as_uuid

from .models import (
    CandidateProfile,
    CandidateSkill,
    ProfileExperience,
    Visibility,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

#: Distinguishes "not looked up yet" from "looked up, and there is none".
_UNSET = object()

#: The related sets every profile screen renders. Listed once so a view cannot
#: accidentally render the page with eleven queries per section.
_SECTIONS = (
    "education",
    "projects",
    "certifications",
    "languages",
    "links",
    "awards",
)


def profile_for(user: User) -> CandidateProfile | None:
    return CandidateProfile.objects.select_related("location", "user").filter(user=user).first()


#: Where `profile_for_request` keeps its answer. One name, so a view that has
#: already resolved the profile can hand it over rather than let the shell ask
#: again.
_REQUEST_CACHE = "_careerai_candidate_profile"


def profile_for_request(request) -> CandidateProfile | None:
    """This request's candidate profile, resolved once.

    The application shell reads it twice on every candidate screen — the
    greeting's headline and the rail's saved-job count — and the view that owns
    the page has usually resolved it already. Three identical lookups per
    render is what that cost before, and on the CV builder screens it was three
    of the queries NFR-2 budgets twenty-five of.

    Returns `None` for an anonymous visitor and for an account that has no
    candidate profile, which is a real state: an employer account never gets
    one.
    """
    cached = getattr(request, _REQUEST_CACHE, _UNSET)
    if cached is not _UNSET:
        return cached  # type: ignore[return-value]

    user = getattr(request, "user", None)
    profile = profile_for(user) if user is not None and user.is_authenticated else None
    remember_profile(request, profile)
    return profile


def remember_profile(request, profile: CandidateProfile | None) -> None:
    """Share a profile the caller already has with the rest of this request."""
    setattr(request, _REQUEST_CACHE, profile)


def full_profile_for(user: User) -> CandidateProfile | None:
    """The profile with every section loaded, for the profile screen."""
    queryset = CandidateProfile.objects.select_related("location", "user").prefetch_related(
        Prefetch(
            "skills",
            queryset=CandidateSkill.objects.select_related("skill", "skill__category"),
        ),
        Prefetch(
            "experiences",
            queryset=ProfileExperience.objects.select_related("location", "company"),
        ),
        "languages__language",
        *_SECTIONS,
    )
    return queryset.filter(user=user).first()


def skills_for(profile: CandidateProfile) -> QuerySet[CandidateSkill]:
    return profile.skills.select_related("skill").all()


def blocked_company_ids(profile: CandidateProfile) -> list[int]:
    return list(profile.blocked_companies.values_list("company_id", flat=True))


def visible_profile(public_id: str, *, viewer: User | None) -> CandidateProfile | None:
    """A profile as seen by someone else.

    Returns None rather than raising, so a caller decides between 404 and a
    "this profile is private" page. `employers` visibility resolves to nothing
    here on purpose: reading it requires a `CandidateVisibilityGrant`, which
    arrives with applications in P8 — until then the honest answer is no, not
    a guess that happens to be permissive.
    """
    if as_uuid(public_id) is None:
        return None
    profile = (
        CandidateProfile.objects.select_related("location", "user")
        .filter(public_id=public_id)
        .first()
    )
    if profile is None:
        return None

    if viewer is not None and profile.user_id == viewer.pk:
        return profile
    if profile.visibility == Visibility.EVERYONE:
        return profile
    return None
