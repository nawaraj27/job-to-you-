"""
Talent pools (FR-6.9).

A pool is how a company keeps track of a person they liked but did not hire —
the "good, wrong role" case that otherwise lives in somebody's inbox.

Adding someone to a pool grants visibility on the `pool` basis (INV-4), and
that grant is the whole point of the feature being modelled rather than being a
list of names: a pooled candidate can be read later, and the record says why.
Removing them revokes it, unless another basis still stands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotFound

from ..models import CandidateVisibilityGrant, GrantBasis, TalentPool, TalentPoolMember
from . import visibility

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company

MAX_POOLS_PER_COMPANY = 40


@transaction.atomic
def create_pool(*, company: Company, actor: User, name: str, description: str = "") -> TalentPool:
    label = (name or "").strip()
    if not label:
        raise InvalidState("Give the pool a name.")
    if TalentPool.objects.filter(company=company, name__iexact=label).exists():
        raise Conflict("You already have a pool with that name.")
    if TalentPool.objects.filter(company=company).count() >= MAX_POOLS_PER_COMPANY:
        raise InvalidState(f"A company keeps up to {MAX_POOLS_PER_COMPANY} pools.")

    pool = TalentPool.objects.create(
        company=company,
        name=label[:80],
        description=(description or "").strip()[:200],
        created_by=actor,
    )
    audit.record(verb="pool.created", actor=actor, company=company, metadata={"pool": pool.name})
    return pool


@transaction.atomic
def delete_pool(*, pool: TalentPool, actor: User) -> None:
    """Deleting a pool revokes the visibility it granted.

    Otherwise a company keeps reading people on the strength of a list they
    deleted, which is the kind of leftover permission nobody ever audits.
    """
    for member in pool.members.filter(deleted_at__isnull=True).select_related("candidate"):
        _revoke_pool_grant(pool=pool, candidate=member.candidate, actor=actor)

    name = pool.name
    company = pool.company
    pool.delete()
    audit.record(verb="pool.deleted", actor=actor, company=company, metadata={"pool": name})


@transaction.atomic
def add_member(*, pool: TalentPool, candidate: CandidateProfile, actor: User) -> TalentPoolMember:
    """Save a candidate to a pool, and record why the company may read them."""
    member, created = TalentPoolMember.objects.get_or_create(
        pool=pool, candidate=candidate, defaults={"added_by": actor}
    )
    if not created and member.deleted_at is not None:
        member.deleted_at = None
        member.added_by = actor
        member.save(update_fields=["deleted_at", "added_by", "updated_at"])
        created = True

    if created:
        visibility.grant(candidate=candidate, company=pool.company, basis=GrantBasis.POOL)
        audit.record(
            verb="pool.member_added",
            actor=actor,
            target=candidate,
            company=pool.company,
            metadata={"pool": pool.name},
        )
    return member


@transaction.atomic
def remove_member(*, pool: TalentPool, candidate: CandidateProfile, actor: User) -> None:
    member = pool.members.filter(candidate=candidate, deleted_at__isnull=True).first()
    if member is None:
        raise NotFound("That candidate is not in this pool.")

    member.deleted_at = timezone.now()
    member.save(update_fields=["deleted_at", "updated_at"])

    _revoke_pool_grant(pool=pool, candidate=candidate, actor=actor)
    audit.record(
        verb="pool.member_removed",
        actor=actor,
        target=candidate,
        company=pool.company,
        metadata={"pool": pool.name},
    )


def _revoke_pool_grant(*, pool: TalentPool, candidate: CandidateProfile, actor: User) -> None:
    """Drop the `pool` grant, and only if no other pool still holds them.

    Other bases — an application, a public profile — are untouched: this
    revokes the reason that just went away, not every reason there is.
    """
    still_pooled = (
        TalentPoolMember.objects.filter(
            pool__company=pool.company, candidate=candidate, deleted_at__isnull=True
        )
        .exclude(pool=pool)
        .exists()
    )
    if still_pooled:
        return

    CandidateVisibilityGrant.objects.live().filter(
        candidate=candidate, company=pool.company, basis=GrantBasis.POOL
    ).update(revoked_at=timezone.now())
