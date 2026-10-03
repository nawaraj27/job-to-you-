"""
Conversations and messages (SRS §12, ADR-A26).

The rules that make this safe rather than merely present:

  * **Opening needs a live `CandidateVisibilityGrant`** (INV-4). There is no
    cold outreach; a company that has never met a candidate cannot start.
  * **Rate limits are per company *per candidate*, and per company per day.**
    One limit is not enough: a per-candidate limit alone permits a thousand
    people receiving one message each, which is exactly bulk outreach.
  * **A block ends the thread and writes `BlockedCompany`.** Half a block —
    silencing the thread while leaving the profile visible — is the version
    that gets somebody hurt.

Attachments are shared with the recipient by a grant row (ADR-A52) rather than
by loosening file visibility, so a file leaves the sender's control for exactly
one person and for a stated reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.applications.services import visibility
from apps.audit import services as audit
from apps.common import ratelimit
from apps.common.errors import InvalidState, NotAllowed, NotFound
from apps.files import constants as file_constants
from apps.files import services as file_service
from apps.notifications import events

from ..models import (
    Conversation,
    ConversationReport,
    ConversationState,
    Message,
    ReportReason,
    ReportState,
    SenderKind,
)

if TYPE_CHECKING:  # pragma: no cover
    from django.db.models import QuerySet

    from apps.accounts.models import User
    from apps.applications.models import Application
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company, CompanyMembership
    from apps.files.models import Attachment
    from apps.jobs.models import Job

MAX_BODY_LENGTH = 8000
MAX_ATTACHMENTS = 5

#: How long a message may be edited. Long enough to fix a typo, short enough
#: that the other party has probably not read it yet — an editable record is
#: not a record.
EDIT_WINDOW_MINUTES = 15


@transaction.atomic
def open_conversation(
    *,
    company: Company,
    candidate: CandidateProfile,
    actor: User,
    membership: CompanyMembership,
    job: Job | None = None,
    application: Application | None = None,
    subject: str = "",
) -> Conversation:
    """Start a thread with a candidate the company has a reason to see.

    Idempotent per (company, candidate, job): a second attempt returns the
    existing thread rather than creating a duplicate, because two threads about
    one role is how a reply lands where nobody is looking.
    """
    # INV-4. The check that makes cold outreach impossible rather than
    # discouraged.
    visibility.require_access(candidate=candidate, company=company)
    _require_not_blocked(company, candidate)

    if job is not None and job.company_id != company.pk:
        raise NotFound("That posting does not belong to this company.")
    if application is not None and application.job.company_id != company.pk:
        raise NotFound("That application does not belong to this company.")

    existing = Conversation.objects.filter(company=company, candidate=candidate, job=job).first()
    if existing is not None:
        if existing.state == ConversationState.BLOCKED:
            raise NotAllowed("This candidate has blocked your company.")
        if existing.state == ConversationState.ARCHIVED:
            existing.state = ConversationState.OPEN
            existing.save(update_fields=["state", "updated_at"])
        return existing

    conversation = Conversation.objects.create(
        company=company,
        candidate=candidate,
        job=job,
        application=application,
        subject=subject.strip()[:140],
        opened_by=actor,
    )

    events.emit(
        "conversation.opened",
        conversation,
        payload={
            "conversation_public_id": str(conversation.public_id),
            "company_id": company.pk,
        },
    )
    audit.record(
        verb="conversation.opened",
        actor=actor,
        target=candidate,
        company=company,
        metadata={"conversation": str(conversation.public_id)},
    )
    return conversation


def send(
    *,
    conversation: Conversation,
    actor: User,
    sender_kind: str,
    body: str,
    attachments: list[Attachment] | None = None,
    uploads: list | None = None,
) -> Message:
    """Post one message.

    The rate limits are consumed **before** the transaction opens: a limiter is
    a counter in Redis, and rolling a database transaction back does not give
    the count back — but neither should a refused message leave a half-written
    thread.

    `uploads` are files straight from a form (FR-7.5). They are stored only
    after every check has passed, inside the message's transaction, through
    `files.store` — so they get the same type check, size limit and virus scan
    as any other upload, and a refused message leaves no orphaned file.
    """
    if sender_kind not in SenderKind.values:
        raise InvalidState("That is not a kind of sender.")

    text = (body or "").strip()
    if not text:
        raise InvalidState("Write something first.")
    if len(text) > MAX_BODY_LENGTH:
        # Cut to 8,000 characters without a word before: the end of a long
        # message simply never arrived, and the sender had no way to know.
        raise InvalidState(
            f"Messages can be up to {MAX_BODY_LENGTH:,} characters. This one is "
            f"{len(text):,}; shorten it or split it in two."
        )
    if len(attachments or []) + len(uploads or []) > MAX_ATTACHMENTS:
        raise InvalidState(f"Up to {MAX_ATTACHMENTS} attachments per message.")

    _require_sendable(conversation, sender_kind)

    if sender_kind == SenderKind.COMPANY:
        # Two limits, because one is not enough: a per-candidate limit alone
        # permits a thousand people receiving one message each (SRS §12).
        ratelimit.enforce(
            [
                (
                    ratelimit.MESSAGE_PER_COMPANY_CANDIDATE,
                    f"{conversation.company_id}:{conversation.candidate_id}",
                ),
                (ratelimit.MESSAGE_PER_COMPANY_DAILY, str(conversation.company_id)),
            ]
        )

    return _record_message(
        conversation=conversation,
        actor=actor,
        sender_kind=sender_kind,
        body=text[:MAX_BODY_LENGTH],
        attachments=attachments or [],
        uploads=uploads or [],
    )


#: What a message may carry: documents and images, the same set company
#: verification accepts.
ATTACHMENT_TYPES = file_constants.DOCUMENT_TYPES | file_constants.IMAGE_TYPES


@transaction.atomic
def _record_message(
    *,
    conversation: Conversation,
    actor: User,
    sender_kind: str,
    body: str,
    attachments: list[Attachment],
    uploads: list | None = None,
) -> Message:
    attachments = list(attachments)
    for upload in uploads or []:
        attachments.append(
            file_service.store(
                uploaded_file=upload, accepted_types=ATTACHMENT_TYPES, uploaded_by=actor
            )
        )
    message = Message.objects.create(
        conversation=conversation, sender=actor, sender_kind=sender_kind, body=body
    )

    if attachments:
        message.attachments.set(attachments)
        _share_attachments(conversation, message, sender_kind)

    conversation.last_message_at = message.created_at
    # The sender has read their own message by definition; saying so keeps the
    # unread badge off their own screen.
    if sender_kind == SenderKind.COMPANY:
        conversation.company_last_read_at = message.created_at
    elif sender_kind == SenderKind.CANDIDATE:
        conversation.candidate_last_read_at = message.created_at
    conversation.save(
        update_fields=[
            "last_message_at",
            "company_last_read_at",
            "candidate_last_read_at",
            "updated_at",
        ]
    )

    events.emit(
        "message.sent",
        conversation,
        payload={
            "conversation_public_id": str(conversation.public_id),
            "company_id": conversation.company_id,
            "sender_kind": sender_kind,
            # The notification read "New message / You have a new message about
            # a role", with no sender, no role and no link (FR-7.4 asks for a
            # deep link). The recipient is the other side, so the link is theirs.
            **_notification_words(conversation, sender_kind),
        },
    )
    return message


def _notification_words(conversation: Conversation, sender_kind: str) -> dict[str, str]:
    from django.urls import reverse

    if sender_kind == SenderKind.COMPANY:
        from_name = conversation.company.name
        link = reverse("messages:inbox")
    else:
        user = conversation.candidate.user
        from_name = (user.full_name if user is not None else "") or "A candidate"
        link = reverse("employer:messages", args=[conversation.company.slug])
    job = conversation.job
    return {
        "from_name": from_name,
        "about": f"About {job.title}." if job is not None else "General enquiry.",
        "target_url": f"{link}?thread={conversation.public_id}",
    }


def _share_attachments(conversation: Conversation, message: Message, sender_kind: str) -> None:
    """Give the other party a reason to read each file (ADR-A52).

    One grant per recipient per file, rather than making the file readable to
    everyone: a CV attached to a message must not become readable because it
    was once in a thread.
    """
    if sender_kind == SenderKind.COMPANY:
        recipients = [conversation.candidate.user]
    else:
        from apps.accounts.models import User

        recipients = list(User.objects.filter(pk__in=_company_readers(conversation)))

    for attachment in message.attachments.all():
        for recipient in recipients:
            if recipient is None:
                continue
            file_service.share_with(attachment=attachment, user=recipient, reason="message")


def _company_readers(conversation: Conversation) -> list[int]:
    """Company members who may read this thread (decision D-13).

    Everyone who can open the inbox — narrowed, for a thread about an
    application, to the members whose read reaches that application. Every
    active member used to be granted the candidate's files, recruiters on other
    jobs included.
    """
    from apps.companies import policies as company_policies
    from apps.companies.models import CompanyMembership

    ids = [
        member.user_id
        for member in CompanyMembership.objects.active()
        .filter(company_id=conversation.company_id)
        .select_related("company")
        if company_policies.can_open_section(member, "messages")
    ]
    if conversation.application is not None:
        from apps.applications import policies as application_policies

        readers = set(application_policies.reader_user_ids(conversation.application))
        ids = [user_id for user_id in ids if user_id in readers]
    return ids


def share_thread_files_with(*, conversation: Conversation, user: User) -> None:
    """Catch a company reader up on the candidate's files in one thread.

    Grants are written when a message is sent, to the people who could read the
    thread then. A recruiter assigned to the job afterwards, or a member who
    joined later, opens the thread through the scoped selector and would find
    every earlier file refusing them. Called only after that selector has
    authorised the read.
    """
    if user.pk not in _company_readers(conversation):
        return
    attachments = _candidate_files(conversation)
    for attachment in attachments:
        file_service.share_with(attachment=attachment, user=user, reason="message")


def _candidate_files(conversation: Conversation) -> QuerySet[Attachment]:
    from apps.files.models import Attachment

    return Attachment.objects.filter(
        messages__conversation=conversation, messages__sender_kind=SenderKind.CANDIDATE
    ).distinct()


@transaction.atomic
def mark_read(*, conversation: Conversation, side: str) -> Conversation:
    """Record that one side has caught up."""
    if side not in {SenderKind.COMPANY, SenderKind.CANDIDATE}:
        raise InvalidState("A conversation has two sides.")

    field = "company_last_read_at" if side == SenderKind.COMPANY else "candidate_last_read_at"
    setattr(conversation, field, timezone.now())
    conversation.save(update_fields=[field, "updated_at"])
    return conversation


@transaction.atomic
def set_mute(*, conversation: Conversation, side: str, muted: bool) -> Conversation:
    """Silence notifications for one side without ending the thread.

    Muting is reversible and invisible to the other party. Blocking is neither,
    and the screens keep them far apart for that reason.
    """
    if side not in {SenderKind.COMPANY, SenderKind.CANDIDATE}:
        raise InvalidState("A conversation has two sides.")

    field = "company_muted_at" if side == SenderKind.COMPANY else "candidate_muted_at"
    setattr(conversation, field, timezone.now() if muted else None)
    conversation.save(update_fields=[field, "updated_at"])
    return conversation


@transaction.atomic
def archive(*, conversation: Conversation, actor: User) -> Conversation:
    """Out of the working list. Reopened by the next message."""
    if conversation.state == ConversationState.BLOCKED:
        raise InvalidState("A blocked conversation cannot be archived.")

    conversation.state = ConversationState.ARCHIVED
    conversation.save(update_fields=["state", "updated_at"])
    return conversation


@transaction.atomic
def block(*, conversation: Conversation, actor: User, reason: str = "") -> Conversation:
    """The candidate ends it, and the company loses sight of them.

    Three effects, and all three are the point: the thread closes, a
    `BlockedCompany` row hides the candidate from that company everywhere else,
    and every visibility grant they held is revoked. A block that left the
    profile readable would be a setting, not a boundary.
    """
    if conversation.candidate.user_id != actor.pk:
        raise NotAllowed("Only the candidate can block a company.")

    # One implementation of what a block means, shared with the privacy screen:
    # the row, the revoked grants and the closed threads all happen there.
    from apps.candidates.services import blocking

    blocking.block_company(
        profile=conversation.candidate,
        actor=actor,
        company=conversation.company,
        reason=reason,
    )
    conversation.refresh_from_db()

    events.emit(
        "conversation.blocked",
        conversation,
        payload={
            "conversation_public_id": str(conversation.public_id),
            "company_id": conversation.company_id,
        },
    )
    audit.record(
        verb="conversation.blocked",
        actor=actor,
        target=conversation.candidate,
        company=conversation.company,
    )
    return conversation


@transaction.atomic
def report(
    *, conversation: Conversation, actor: User, reason: str, detail: str = ""
) -> ConversationReport:
    """Send a thread to the staff moderation queue.

    Open to both sides. A report from an employer about an abusive candidate is
    as real as the reverse, and a product that only accepts one direction has
    decided in advance who the problem is.
    """
    if reason not in ReportReason.values:
        raise InvalidState("Choose a reason.")
    if not _is_participant(conversation, actor):
        raise NotFound("That conversation does not exist, or you cannot see it.")

    record = ConversationReport.objects.create(
        conversation=conversation,
        reporter=actor,
        reason=reason,
        detail=(detail or "").strip()[:2000],
    )

    events.emit(
        "conversation.reported",
        conversation,
        payload={
            "conversation_public_id": str(conversation.public_id),
            "company_id": conversation.company_id,
            "reason": reason,
        },
    )
    audit.record(
        verb="conversation.reported",
        actor=actor,
        target=conversation.candidate,
        company=conversation.company,
        metadata={"reason": reason},
    )
    return record


@transaction.atomic
def resolve_report(
    *, report_record: ConversationReport, actor: User, state: str, resolution: str = ""
) -> ConversationReport:
    """A staff decision. Dismissed reports are kept, not deleted.

    A company with three dismissed reports and a company with none are
    different companies, and that difference is only visible if the dismissed
    ones survive.
    """
    if state not in {ReportState.REVIEWING, ReportState.UPHELD, ReportState.DISMISSED}:
        raise InvalidState("That is not a review outcome.")
    if state != ReportState.REVIEWING and not resolution.strip():
        raise InvalidState("Say what was decided.")

    report_record.state = state
    report_record.resolution = resolution.strip()[:300]
    report_record.reviewed_by = actor
    report_record.reviewed_at = timezone.now()
    report_record.save(
        update_fields=["state", "resolution", "reviewed_by", "reviewed_at", "updated_at"]
    )

    audit.record(
        verb="conversation.report_reviewed",
        actor=actor,
        target=report_record.conversation.candidate,
        company=report_record.conversation.company,
        metadata={"state": state},
    )
    return report_record


@transaction.atomic
def edit(*, message: Message, actor: User, body: str) -> Message:
    """Fix a typo, inside a short window.

    Not an unlimited edit: the other party may already have read it, and a
    thread whose history can be rewritten is not a record of anything.
    """
    if message.sender_id != actor.pk:
        raise NotAllowed("You can only edit your own messages.")
    if message.deleted_at is not None:
        raise InvalidState("That message was deleted.")

    import datetime as dt

    if timezone.now() - message.created_at > dt.timedelta(minutes=EDIT_WINDOW_MINUTES):
        raise InvalidState(
            f"Messages can be edited for {EDIT_WINDOW_MINUTES} minutes after sending."
        )

    text = (body or "").strip()
    if not text:
        raise InvalidState("Write something first.")

    message.body = text[:MAX_BODY_LENGTH]
    message.edited_at = timezone.now()
    message.save(update_fields=["body", "edited_at", "updated_at"])
    return message


@transaction.atomic
def delete(*, message: Message, actor: User) -> Message:
    """Leave a tombstone, not a hole."""
    if message.sender_id != actor.pk:
        raise NotAllowed("You can only delete your own messages.")

    message.deleted_at = timezone.now()
    message.save(update_fields=["deleted_at", "updated_at"])

    for attachment in message.attachments.all():
        _revoke_attachment(message, attachment)
    return message


def _revoke_attachment(message: Message, attachment: Attachment) -> None:
    """Deleting a message takes back what it shared."""
    conversation = message.conversation
    if message.sender_kind == SenderKind.COMPANY:
        recipients = [conversation.candidate.user]
    else:
        from apps.companies.models import CompanyMembership

        recipients = [
            membership.user
            for membership in CompanyMembership.objects.active().filter(
                company_id=conversation.company_id
            )
        ]

    for recipient in recipients:
        if recipient is not None:
            file_service.revoke_share(attachment=attachment, user=recipient, reason="message")


# --- guards ---------------------------------------------------------------


def _require_not_blocked(company: Company, candidate: CandidateProfile) -> None:
    if candidate.blocked_companies.filter(company=company).exists():
        raise NotAllowed("This candidate has blocked your company.")


def _require_sendable(conversation: Conversation, sender_kind: str) -> None:
    if conversation.state == ConversationState.BLOCKED:
        raise NotAllowed("This conversation is blocked.")
    if not conversation.company.is_active:
        # Suspension "freezes messaging" (§2.6), in both directions. Only the
        # company side was stopped, by its members losing access; a candidate
        # kept writing into a thread nobody could read.
        raise NotAllowed("This company's account is paused, so messages cannot be sent.")
    if sender_kind == SenderKind.COMPANY:
        _require_not_blocked(conversation.company, conversation.candidate)
        # The reason to be in touch has to still exist. A company that lost
        # every grant should not keep messaging on the strength of a thread
        # opened months ago.
        visibility.require_access(candidate=conversation.candidate, company=conversation.company)
    if conversation.state == ConversationState.ARCHIVED:
        conversation.state = ConversationState.OPEN
        conversation.save(update_fields=["state", "updated_at"])


def _is_participant(conversation: Conversation, actor: User) -> bool:
    if conversation.candidate.user_id == actor.pk:
        return True

    from apps.companies.models import CompanyMembership

    return (
        CompanyMembership.objects.active()
        .filter(company_id=conversation.company_id, user=actor)
        .exists()
    )
