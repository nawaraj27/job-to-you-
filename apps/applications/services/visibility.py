"""
Candidate visibility grants (INV-4).

Every employer-side read of a candidate is authorised by a row here. The
alternative — inferring permission from "they have an application somewhere" —
is a rule that lives in whichever queryset last remembered it, and its failure
is silent: a company reading a person who never dealt with them.

Grants are additive and revocable. Applying grants one; blocking the company
revokes every one they hold, which is what a block has to mean if it means
anything.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.audit import services as audit

from ..models import Application, CandidateVisibilityGrant, GrantBasis

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company


@transaction.atomic
def grant(
    *,
    candidate: CandidateProfile,
    company: Company,
    basis: str,
    application: Application | None = None,
    expires_at: dt.datetime | None = None,
) -> CandidateVisibilityGrant:
    """Record why this company may read this candidate. Idempotent per basis."""
    if basis not in GrantBasis.values:
        from apps.common.errors import InvalidState

        raise InvalidState("That is not a basis for seeing a candidate.")
    if candidate.blocked_companies.filter(company=company).exists():
        # A block revokes every grant (`revoke_all`), and nothing may write a
        # new one afterwards: saving a blocked candidate to a pool from their
        # old application quietly re-created a live `pool` grant.
        from apps.common.errors import NotAllowed

        raise NotAllowed("This candidate has blocked your company.")

    existing = (
        CandidateVisibilityGrant.objects.live()
        .filter(candidate=candidate, company=company, basis=basis, application=application)
        .first()
    )
    if existing is not None:
        return existing

    return CandidateVisibilityGrant.objects.create(
        candidate=candidate,
        company=company,
        basis=basis,
        application=application,
        granted_at=timezone.now(),
        expires_at=expires_at,
    )


def has_access(*, candidate: CandidateProfile, company: Company) -> bool:
    """The question every employer-side candidate read must ask first."""
    return (
        CandidateVisibilityGrant.objects.live()
        .filter(candidate=candidate, company=company)
        .exists()
    )


def require_access(*, candidate: CandidateProfile, company: Company) -> None:
    """NotFound rather than NotAllowed.

    A 403 confirms that a person with this id exists on the platform, which is
    exactly what a company with no grant must not learn.
    """
    if not has_access(candidate=candidate, company=company):
        from apps.common.errors import NotFound

        raise NotFound("That candidate does not exist, or you cannot see them.")


@transaction.atomic
def revoke_all(*, candidate: CandidateProfile, company: Company, actor: User | None = None) -> int:
    """Withdraw everything this company holds on this candidate.

    Called when the candidate blocks them. A block that left live grants
    standing would be a setting rather than a boundary.
    """
    revoked = (
        CandidateVisibilityGrant.objects.live()
        .filter(candidate=candidate, company=company)
        .update(revoked_at=timezone.now())
    )
    if revoked:
        audit.record(
            verb="candidate.visibility_revoked",
            actor=actor,
            target=candidate,
            company=company,
            metadata={"grants": revoked},
        )
    return revoked


def companies_with_access(candidate: CandidateProfile) -> QuerySet[CandidateVisibilityGrant]:
    """Who can see me, and why. The candidate's own answer to that question."""
    return (
        CandidateVisibilityGrant.objects.live()
        .filter(candidate=candidate)
        .select_related("company", "application", "application__job")
        .order_by("-granted_at")
    )
