"""
Authorization for messaging (SRS §12, INV-4).

Two participants and no others. Everything refuses with NotFound across the
boundary: a 403 on a thread confirms that this company and this candidate are
talking, which is itself information neither of them agreed to publish.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.common.errors import NotAllowed, NotFound

from .models import Conversation, ConversationState, SenderKind

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import CompanyMembership


def can_read_as_company(conversation: Conversation, membership: CompanyMembership | None) -> bool:
    if membership is None or not membership.is_active:
        return False
    return membership.company_id == conversation.company_id


def require_company_side(
    conversation: Conversation, membership: CompanyMembership | None
) -> Conversation:
    if not can_read_as_company(conversation, membership):
        raise NotFound("That conversation does not exist, or you cannot see it.")
    return conversation


def require_candidate_side(
    conversation: Conversation, profile: CandidateProfile | None
) -> Conversation:
    if profile is None or conversation.candidate_id != profile.pk:
        raise NotFound("That conversation does not exist, or you cannot see it.")
    return conversation


def side_for(conversation: Conversation, actor: User) -> str:
    """Which side this actor is on, refusing if they are on neither."""
    if conversation.candidate.user_id == actor.pk:
        return SenderKind.CANDIDATE

    from apps.companies.models import CompanyMembership

    if (
        CompanyMembership.objects.active()
        .filter(company_id=conversation.company_id, user=actor)
        .exists()
    ):
        return SenderKind.COMPANY

    raise NotFound("That conversation does not exist, or you cannot see it.")


def require_open(conversation: Conversation) -> Conversation:
    """A blocked thread takes no more messages, from either side."""
    if conversation.state == ConversationState.BLOCKED:
        raise NotAllowed("This conversation is blocked.")
    return conversation
