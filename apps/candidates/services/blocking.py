"""
Blocking a company (SRS §5.6, §12).

One implementation of what a block *means*, because there are two places to
perform one — the privacy screen and a conversation — and two implementations
would eventually disagree about whether a block also withdraws profile access.
It does. Three effects, always:

  1. a `BlockedCompany` row, which hides the candidate from that company and
     that company's jobs from the candidate;
  2. every `CandidateVisibilityGrant` they hold is revoked (INV-4), so the
     block reaches the profile and not only the inbox;
  3. any open conversation is closed.

A block that left the profile readable would be a setting rather than a
boundary, and the person who set it would not find that out until it mattered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit

from ..models import BlockedCompany

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company

    from ..models import CandidateProfile


@transaction.atomic
def block_company(
    *, profile: CandidateProfile, actor: User, company: Company, reason: str = ""
) -> BlockedCompany:
    """Cut a company off. Idempotent."""
    record, created = BlockedCompany.objects.get_or_create(
        profile=profile, company=company, defaults={"reason": reason.strip()[:200]}
    )

    # Imported here rather than at module scope: `candidates` is a lower layer
    # than `applications` and `messaging`, and a top-level import would make
    # that dependency permanent in the wrong direction.
    from apps.applications.services import visibility
    from apps.messaging.models import Conversation, ConversationState

    visibility.revoke_all(candidate=profile, company=company, actor=actor)
    # Out of that company's talent pools too: the pool page listed a candidate
    # by name after they had blocked the company.
    from apps.applications.models import TalentPoolMember

    TalentPoolMember.objects.filter(
        pool__company=company, candidate=profile, deleted_at__isnull=True
    ).update(deleted_at=timezone.now())
    Conversation.objects.filter(candidate=profile, company=company).exclude(
        state=ConversationState.BLOCKED
    ).update(state=ConversationState.BLOCKED)

    if created:
        audit.record(
            verb="company.blocked",
            actor=actor,
            target=profile,
            company=company,
            metadata={"reason": record.reason},
        )
    return record


@transaction.atomic
def unblock_company(*, profile: CandidateProfile, actor: User, company: Company) -> int:
    """Lift a block.

    Deliberately **not** symmetrical: the visibility grants that were revoked
    are not restored. If the company still has a reason to see this candidate —
    a live application, a pool — that reason will create a fresh grant. Reviving
    a revoked grant would resurrect access the candidate had already withdrawn,
    which is not what "unblock" asks for.
    """
    removed, _ = BlockedCompany.objects.filter(profile=profile, company=company).delete()
    if removed:
        audit.record(verb="company.unblocked", actor=actor, target=profile, company=company)
    return removed
