"""
Messaging (SRS §5.9, §12, ADR-A26).

The conversation is where hiring actually happens, and a platform that cannot
see it cannot measure, protect or improve it. Three decisions shape the tables:

  * **There is no `ConversationParticipant` table.** A conversation is
    permanently one company and one candidate, so a join table for a fixed
    two-party relation is generality with no case behind it. Read and mute
    state are columns, one pair per side (SRS §5.9).
  * **A conversation cannot exist without a reason to exist.** Opening one
    requires a live `CandidateVisibilityGrant` (INV-4), which is what makes
    cold outreach structurally impossible rather than merely discouraged.
  * **Blocking is not muting.** Muting silences notifications; blocking ends
    the conversation and writes a `BlockedCompany` row, which hides the
    candidate from that company everywhere else too.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import PublicIdModel, TimeStampedModel


class ConversationState(models.TextChoices):
    OPEN = "open", _("Open")
    ARCHIVED = "archived", _("Archived")
    BLOCKED = "blocked", _("Blocked by the candidate")


class ConversationQuerySet(models.QuerySet):
    def for_company(self, company) -> ConversationQuerySet:
        """The tenant boundary. Every employer-side read starts here."""
        return self.filter(company=company)

    def for_candidate(self, profile) -> ConversationQuerySet:
        return self.filter(candidate=profile)

    def active(self) -> ConversationQuerySet:
        return self.filter(state=ConversationState.OPEN)


class Conversation(PublicIdModel, TimeStampedModel):
    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="conversations"
    )
    candidate = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="conversations"
    )

    # Anchors. Both optional, both by design: a conversation about "that
    # backend role" and a conversation about one application are different
    # threads, and a conversation about neither is a legitimate third case.
    job = models.ForeignKey(
        "jobs.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="conversations"
    )
    application = models.ForeignKey(
        "applications.Application",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="conversations",
    )

    state = models.CharField(
        max_length=16, choices=ConversationState.choices, default=ConversationState.OPEN
    )
    subject = models.CharField(max_length=140, blank=True)
    opened_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    last_message_at = models.DateTimeField(null=True, blank=True)

    # One pair of columns per side rather than a participant table. Read state
    # is a timestamp, not a counter: a counter has to be decremented by
    # somebody, and the somebody is always missing in one code path.
    company_last_read_at = models.DateTimeField(null=True, blank=True)
    candidate_last_read_at = models.DateTimeField(null=True, blank=True)
    company_muted_at = models.DateTimeField(null=True, blank=True)
    candidate_muted_at = models.DateTimeField(null=True, blank=True)

    objects = ConversationQuerySet.as_manager()

    class Meta:
        ordering = ("-last_message_at", "-created_at")
        constraints = [
            # One thread per (company, candidate, job). Two threads about one
            # role is how a reply lands where nobody is looking.
            models.UniqueConstraint(
                fields=["company", "candidate", "job"],
                name="conversation_one_per_job",
                condition=models.Q(job__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["company", "candidate"],
                name="conversation_one_general",
                condition=models.Q(job__isnull=True),
            ),
        ]
        indexes = [
            models.Index(fields=["company", "-last_message_at"], name="conversation_company_idx"),
            models.Index(
                fields=["candidate", "-last_message_at"], name="conversation_candidate_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.company_id} ↔ {self.candidate_id}"

    @property
    def is_open(self) -> bool:
        return self.state == ConversationState.OPEN

    @property
    def is_blocked(self) -> bool:
        return self.state == ConversationState.BLOCKED

    def unread_for_company(self) -> int:
        # Read from the list query's annotation when there is one: calling
        # this per thread ran one COUNT per row of the inbox.
        annotated = getattr(self, "unread_company_count", None)
        if annotated is not None:
            return annotated
        return self._unread(self.company_last_read_at, exclude_company=True)

    def unread_for_candidate(self) -> int:
        annotated = getattr(self, "unread_candidate_count", None)
        if annotated is not None:
            return annotated
        return self._unread(self.candidate_last_read_at, exclude_company=False)

    def _unread(self, since, *, exclude_company: bool) -> int:
        """Messages the other side sent that this side has not read.

        Counted from the message rows rather than kept as a column: a stored
        counter is one missed decrement away from a badge nobody can clear.
        """
        queryset = self.messages.filter(deleted_at__isnull=True)
        queryset = (
            queryset.exclude(sender_kind=SenderKind.COMPANY)
            if exclude_company
            else queryset.exclude(sender_kind=SenderKind.CANDIDATE)
        )
        if since is not None:
            queryset = queryset.filter(created_at__gt=since)
        return queryset.count()


class SenderKind(models.TextChoices):
    """Which side sent it.

    Stored rather than derived from the sender's memberships: a recruiter who
    later leaves the company must not turn their old messages into candidate
    messages, and a person can be both a candidate and an employer elsewhere.
    """

    COMPANY = "company", _("Company")
    CANDIDATE = "candidate", _("Candidate")
    SYSTEM = "system", _("System")


class Message(TimeStampedModel):
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sender_kind = models.CharField(max_length=16, choices=SenderKind.choices)
    body = models.TextField()
    attachments = models.ManyToManyField("files.Attachment", blank=True, related_name="messages")

    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at", "pk")
        indexes = [
            models.Index(fields=["conversation", "-created_at"], name="message_conversation_idx")
        ]

    def __str__(self) -> str:
        return self.body[:60]

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def display_body(self) -> str:
        """What a deleted message reads as.

        A tombstone rather than removal: the other party already read it, and
        a message that vanishes from a thread makes the conversation a record
        nobody can rely on.
        """
        return "This message was deleted." if self.is_deleted else self.body


class ReportReason(models.TextChoices):
    SPAM = "spam", _("Spam or bulk outreach")
    HARASSMENT = "harassment", _("Harassment or abuse")
    DISCRIMINATION = "discrimination", _("Discriminatory content")
    SCAM = "scam", _("Scam, fee request or fake role")
    OTHER = "other", _("Something else")


class ReportState(models.TextChoices):
    OPEN = "open", _("Awaiting review")
    REVIEWING = "reviewing", _("Under review")
    UPHELD = "upheld", _("Upheld")
    DISMISSED = "dismissed", _("Dismissed")


class ConversationReport(TimeStampedModel):
    """A report from either side, reviewed by staff.

    Kept even after review: a company with three dismissed reports and a
    company with none are different companies, and that difference is only
    visible if dismissed reports survive.
    """

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="reports")
    reporter = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reason = models.CharField(max_length=20, choices=ReportReason.choices)
    detail = models.TextField(blank=True)

    state = models.CharField(max_length=16, choices=ReportState.choices, default=ReportState.OPEN)
    resolution = models.CharField(max_length=300, blank=True)
    reviewed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["state", "-created_at"],
                condition=models.Q(state__in=["open", "reviewing"]),
                name="conversationreport_queue_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.reason} on {self.conversation_id}"

    @property
    def is_open(self) -> bool:
        return self.state in {ReportState.OPEN, ReportState.REVIEWING}

    @property
    def age_hours(self) -> float:
        return (timezone.now() - self.created_at).total_seconds() / 3600
