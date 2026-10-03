"""
Messaging reads (DEVELOPER.md §1.2).

Scoped in the query on both sides: a company reads `for_company`, a candidate
reads `for_candidate`, and there is no third path into these rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.db.models import Count, F, Q, QuerySet

from apps.common.ids import as_uuid

from .models import Conversation, ConversationReport, Message, ReportState

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company, CompanyMembership


def _unread_filter(*, reader: str) -> Q:
    """Messages from the other side, not deleted, newer than this side's read mark.

    The same rule as `Conversation._unread`, expressed once for a whole list so
    the inbox is one query however many threads it shows.
    """
    from .models import SenderKind

    own = SenderKind.COMPANY if reader == "company" else SenderKind.CANDIDATE
    read_at = "company_last_read_at" if reader == "company" else "candidate_last_read_at"
    return (
        Q(messages__deleted_at__isnull=True)
        & ~Q(messages__sender_kind=own)
        & (Q(**{f"{read_at}__isnull": True}) | Q(messages__created_at__gt=F(read_at)))
    )


def _company_threads(membership: CompanyMembership) -> QuerySet[Conversation]:
    """The company's threads this member may read (decision D-13).

    A thread started from an application is part of that applicant's record,
    so it follows the application's read scope: a recruiter sees the threads
    for the jobs they are assigned to. A thread from a talent search belongs to
    no job's applicants and stays with everyone who can open the inbox.
    """
    from apps.applications.models import Application

    return Conversation.objects.for_company(membership.company).filter(
        Q(application__isnull=True)
        | Q(application__in=Application.objects.visible_to(membership).values("pk"))
    )


def conversations_for_company(membership: CompanyMembership) -> QuerySet[Conversation]:
    # cast: `annotate` loses the model type in django-stubs; the rows are
    # still Conversations.
    return cast(
        "QuerySet[Conversation]",
        _company_threads(membership)
        .select_related("candidate", "candidate__user", "job", "application")
        .annotate(unread_company_count=Count("messages", filter=_unread_filter(reader="company")))
        .order_by("-last_message_at", "-created_at"),
    )


def _valid_id(public_id: str) -> bool:
    """Whether a thread id from a URL or form can be a public id at all.

    A malformed `?thread=` reached the query and raised `ValidationError`, so a
    mistyped or truncated link was a 500 on both inboxes rather than "no such
    conversation".
    """
    return as_uuid(public_id) is not None


def conversation_for_company(membership: CompanyMembership, public_id: str) -> Conversation | None:
    if as_uuid(public_id) is None:
        return None
    if not _valid_id(public_id):
        return None
    return (
        _company_threads(membership)
        .select_related("company", "candidate", "candidate__user", "job", "application")
        .filter(public_id=public_id)
        .first()
    )


def conversations_for_candidate(profile: CandidateProfile) -> QuerySet[Conversation]:
    return cast(
        "QuerySet[Conversation]",
        Conversation.objects.for_candidate(profile)
        .select_related("company", "job")
        .annotate(
            unread_candidate_count=Count("messages", filter=_unread_filter(reader="candidate"))
        )
        .order_by("-last_message_at", "-created_at"),
    )


def conversation_for_candidate(profile: CandidateProfile, public_id: str) -> Conversation | None:
    if as_uuid(public_id) is None:
        return None
    if not _valid_id(public_id):
        return None
    return (
        Conversation.objects.for_candidate(profile)
        .select_related("company", "candidate", "candidate__user", "job", "application")
        .filter(public_id=public_id)
        .first()
    )


def messages_in(conversation: Conversation) -> QuerySet[Message]:
    """Every message, deleted ones included as tombstones.

    Excluding them would leave a thread that reads as though nothing was ever
    said, which is worse than a line saying somebody deleted something.
    """
    return conversation.messages.select_related("sender").prefetch_related("attachments")


def unread_count_for_candidate(profile: CandidateProfile) -> int:
    """One number for the rail badge, across every thread."""
    # One aggregate. The loop ran a COUNT per thread on every page that shows
    # the navigation badge.
    return (
        Conversation.objects.for_candidate(profile)
        .exclude(state="blocked")
        .aggregate(total=Count("messages", filter=_unread_filter(reader="candidate")))["total"]
        or 0
    )


def unread_count_for_company(membership: CompanyMembership) -> int:
    return (
        _company_threads(membership)
        .exclude(state="blocked")
        .aggregate(total=Count("messages", filter=_unread_filter(reader="company")))["total"]
        or 0
    )


def open_reports() -> QuerySet[ConversationReport]:
    """The staff moderation queue, oldest first: a report that has waited
    longest is the one most likely to have hurt somebody."""
    return (
        ConversationReport.objects.filter(
            Q(state=ReportState.OPEN) | Q(state=ReportState.REVIEWING)
        )
        .select_related("conversation", "conversation__company", "reporter")
        .order_by("created_at")
    )


def reports_for(company: Company) -> QuerySet[ConversationReport]:
    return ConversationReport.objects.filter(conversation__company=company)
