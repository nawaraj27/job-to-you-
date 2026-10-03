"""
Object-level authorization for candidate data (SRS §2.5, layer 3).

Short, because the rule is short: a candidate profile has exactly one owner and
nobody else may write it. The value is in refusing to let that rule be restated
per view — every service call routes through `require_owner`, so there is one
place to read and one place to break.

Every rule here has a passing negative test (DEVELOPER.md §8.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.common.errors import NotAllowed, NotFound

from .models import CandidateProfile, Visibility

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User


def is_owner(profile: CandidateProfile, actor: User | None) -> bool:
    return actor is not None and actor.is_authenticated and profile.user_id == actor.pk


def require_owner(profile: CandidateProfile, actor: User | None) -> CandidateProfile:
    """Writes are the owner's alone.

    NotFound rather than NotAllowed: a 403 on someone else's profile confirms
    that the profile exists, which is itself the leak (SRS §2.2).
    """
    if not is_owner(profile, actor):
        raise NotFound("That profile does not exist, or you cannot see it.")
    return profile


def require_can_view(profile: CandidateProfile, viewer: User | None) -> CandidateProfile:
    """Reads by anyone other than the owner.

    `employers` visibility is refused here until visibility grants exist (P8).
    Defaulting to permissive while the check is unfinished is how a private
    profile becomes a public one for a release.
    """
    if is_owner(profile, viewer):
        return profile
    if profile.visibility == Visibility.EVERYONE:
        return profile
    raise NotFound("That profile does not exist, or you cannot see it.")


def require_indexable(profile: CandidateProfile) -> None:
    """Search-engine indexing is a second opt-in on top of a public profile.

    Two switches rather than one, because "employers can find me" and "my name
    is in Google forever" are different decisions and a candidate who conflated
    them cannot undo the second.
    """
    if profile.visibility != Visibility.EVERYONE or not profile.search_indexing:
        raise NotAllowed("This profile is not indexable.")
