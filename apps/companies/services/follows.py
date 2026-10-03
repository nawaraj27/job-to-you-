"""
Following a company (ledger §11, FOL-01).

A follow is one person asking to hear when one company posts a job. It is
deliberately small: the relationship, and the rules about when it may exist.
What happens *because* of it lives in the notification system — `job.published`
names the `company_followers` audience, and the outbox fan-out does the rest —
so there is no second path by which a company can reach somebody.

Three rules:

  * **Only a live, public company can be followed.** A suspended or removed
    company has no public page (SRS §3.7), so there is nothing to follow and
    nothing it may announce.
  * **Blocking beats following.** A candidate who blocked a company asked for
    it and them to be invisible to each other. Following it would be the
    opposite request, so it is refused rather than silently kept, and the
    audience excludes blocked pairs anyway in case the block came later.
  * **Idempotent both ways.** Two tabs pressing "Follow" is one follow;
    unfollowing something not followed is not an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from apps.audit import services as audit
from apps.common.errors import InvalidState, NotFound

from ..models import Company, CompanyFollow, CompanyState

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User


def is_following(*, user: User, company: Company) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return CompanyFollow.objects.filter(user=user, company=company).exists()


def follower_count(company: Company) -> int:
    return CompanyFollow.objects.filter(company=company).count()


def follow(*, user: User, company: Company) -> CompanyFollow:
    """Start following. Returns the follow, new or existing."""
    _require_followable(company)
    _refuse_if_blocked(user=user, company=company)

    existing = CompanyFollow.objects.filter(user=user, company=company).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            record = CompanyFollow.objects.create(user=user, company=company)
    except IntegrityError:
        # The other tab won. Same outcome, so the same answer.
        return CompanyFollow.objects.get(user=user, company=company)

    audit.record(verb="company.followed", actor=user, target=company)
    return record


def unfollow(*, user: User, company: Company) -> bool:
    """Stop following. Returns whether there was anything to stop.

    Notices already delivered stay: they were true when they were sent.
    """
    deleted, _ = CompanyFollow.objects.filter(user=user, company=company).delete()
    if deleted:
        audit.record(verb="company.unfollowed", actor=user, target=company)
    return bool(deleted)


def _require_followable(company: Company) -> None:
    if company.state != CompanyState.ACTIVE or company.deleted_at is not None:
        # The same answer a missing company gets: a suspended company's
        # existence is not something its public page confirms.
        raise NotFound("That company does not exist, or you cannot see it.")


def _refuse_if_blocked(*, user: User, company: Company) -> None:
    profile = getattr(user, "candidate_profile", None)
    if profile is not None and profile.blocked_companies.filter(company=company).exists():
        raise InvalidState(
            "You have blocked this company. Unblock it in Privacy settings to follow it."
        )
