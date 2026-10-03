"""
Scoped reads for interviews (DEVELOPER.md §1.1).

Every queryset here is scoped in the query rather than filtered afterwards, so
a caller cannot forget: `sessions_for_candidate` cannot return somebody else's
interview, and `sessions_for_company` cannot return a practice session, because
neither is in the queryset to begin with.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.db.models import QuerySet

from apps.common.ids import as_uuid

from .models import (
    InterviewKind,
    InterviewOutcome,
    InterviewSession,
    InterviewSlot,
    InterviewTemplate,
    SessionState,
    SlotState,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company, CompanyMembership


def sessions_for_candidate(profile: CandidateProfile) -> QuerySet[InterviewSession]:
    """Everything this candidate has done, practice included."""
    return (
        InterviewSession.objects.filter(candidate=profile)
        .select_related("template", "application__job__company", "outcome")
        .order_by("-created_at")
    )


def practice_for(profile: CandidateProfile) -> QuerySet[InterviewSession]:
    return sessions_for_candidate(profile).filter(application__isnull=True)


def sessions_for_company(company: Company) -> QuerySet[InterviewSession]:
    """Interviews this company ran.

    `application__isnull=False` is doing real work: without it a company could
    reach a candidate's practice session through a join, which is the one thing
    practice mode promises will not happen.
    """
    return (
        InterviewSession.objects.filter(
            application__isnull=False, application__job__company=company
        )
        .select_related("candidate__user", "application__job", "template", "outcome")
        .order_by("-created_at")
    )


def session_for_candidate(profile: CandidateProfile, public_id: str) -> InterviewSession | None:
    if as_uuid(public_id) is None:
        return None
    return sessions_for_candidate(profile).filter(public_id=public_id).first()


def session_for_company(company: Company, public_id: str) -> InterviewSession | None:
    if as_uuid(public_id) is None:
        return None
    return sessions_for_company(company).filter(public_id=public_id).first()


def turns_for(session: InterviewSession) -> QuerySet:
    return session.turns.order_by("order")


def outcomes_for_application(application) -> QuerySet[InterviewOutcome]:
    return InterviewOutcome.objects.filter(session__application=application).select_related(
        "session"
    )


def slots_for(membership: CompanyMembership) -> QuerySet[InterviewSlot]:
    from django.utils import timezone

    # Times that have not ended yet. A past time nobody picked used to stay on
    # the calendar as "Open" for ever, oldest first — and behind the forty-row
    # cap, enough of them hid the times still to come.
    return (
        InterviewSlot.objects.filter(interviewer=membership, ends_at__gte=timezone.now())
        .exclude(state=SlotState.CANCELLED)
        .select_related("session", "session__candidate")
        .order_by("starts_at")
    )


def upcoming_for_candidate(profile: CandidateProfile) -> QuerySet[InterviewSession]:
    from django.utils import timezone

    # Under way as well as ahead, and for as long as the room can still be open:
    # a candidate who dropped out of the call came back to a hub that no longer
    # listed the interview they were in the middle of.
    return (
        sessions_for_candidate(profile)
        .filter(
            state__in=(SessionState.SCHEDULED, SessionState.IN_PROGRESS),
            kind=InterviewKind.LIVE,
            scheduled_at__gte=timezone.now() - dt.timedelta(hours=3),
        )
        .select_related("slot")
        .order_by("scheduled_at")
    )


def practice_templates() -> QuerySet[InterviewTemplate]:
    """The platform's own practice interviews (FR-10.2).

    `company__isnull=True` is the whole scoping rule: a company's template is
    its own, and a practice run has no employer to belong to.
    """
    return InterviewTemplate.objects.filter(
        company__isnull=True, kind=InterviewKind.AI_TEXT, is_active=True
    )


def practice_template(slug: str) -> InterviewTemplate | None:
    return InterviewTemplate.objects.filter(company__isnull=True, slug=slug).first()


def live_template() -> InterviewTemplate | None:
    """The platform template a scheduled interview is created against."""
    return InterviewTemplate.objects.filter(company__isnull=True, kind=InterviewKind.LIVE).first()
