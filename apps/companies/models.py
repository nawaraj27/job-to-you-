"""
Companies and membership (SRS §5.6).

`CompanyMembership` is the authorization spine (ADR-A1). Authorization is never
a field on `User`: the same person can be a candidate, a recruiter at one
company and the owner of another, simultaneously and permanently, and a role
column cannot express that at any size.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import PublicIdModel, TimeStampedModel

from .permissions import Role, grants_for, validate_grants


class VerificationState(models.TextChoices):
    NOT_SUBMITTED = "not_submitted", _("Not submitted")
    PENDING = "pending", _("In review")
    MORE_INFO = "more_info", _("More information needed")
    VERIFIED = "verified", _("Verified")
    REJECTED = "rejected", _("Rejected")


class CompanyState(models.TextChoices):
    ACTIVE = "active", _("Active")
    SUSPENDED = "suspended", _("Suspended")


class CompanyQuerySet(models.QuerySet):
    def alive(self) -> CompanyQuerySet:
        return self.filter(deleted_at__isnull=True)

    def for_actor(self, user) -> CompanyQuerySet:
        """Companies this user belongs to. The only entry point for a portal.

        Takes a user, not a request (SRS §7.2): a scheduled job has an actor and
        no request, and a scoping primitive that needs HTTP is a scoping
        primitive that background work will route around.
        """
        if user is None or not user.is_authenticated:
            return self.none()
        return self.alive().filter(
            memberships__user=user,
            memberships__deleted_at__isnull=True,
            memberships__status=CompanyMembership.Status.ACTIVE,
        )


class Company(PublicIdModel, TimeStampedModel):
    slug = models.SlugField(
        max_length=80,
        unique=True,
        help_text="Immutable once published — it is in every employer URL (ADR-A18).",
    )
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True)
    website = models.URLField(blank=True)
    tagline = models.CharField(max_length=255, blank=True)
    size_band = models.CharField(max_length=20, blank=True)

    # The public profile (SRS §7.1). A company page that carries only a name is
    # the reason candidates open a company's own website instead, and every one
    # of those is an application the platform does not see.
    industry = models.ForeignKey(
        "taxonomy.Industry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    location = models.ForeignKey(
        "taxonomy.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    about = models.JSONField(
        default=list, blank=True, help_text="Paragraphs, in order. Rendered as prose."
    )
    founded = models.CharField(max_length=8, blank=True)
    logo = models.ForeignKey(
        "files.Attachment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    verification_state = models.CharField(
        max_length=20,
        choices=VerificationState.choices,
        default=VerificationState.NOT_SUBMITTED,
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    state = models.CharField(
        max_length=16, choices=CompanyState.choices, default=CompanyState.ACTIVE
    )
    strict_assignment_scoping = models.BooleanField(
        default=False,
        help_text=(
            "When set, recruiters read only applications for jobs assigned to them. "
            "Off by default: a five-person company should not have to assign every "
            "job to every recruiter to make the product usable (SRS §2.3)."
        ),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    row_version = models.PositiveIntegerField(default=0)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = CompanyQuerySet.as_manager()

    class Meta:
        verbose_name_plural = "companies"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None and self.state == CompanyState.ACTIVE

    @property
    def is_verified(self) -> bool:
        return self.verification_state == VerificationState.VERIFIED


class CompanyDomain(models.Model):
    """A verified email domain, used to prove someone works where they claim."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="domains")
    domain = models.CharField(max_length=255)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # A verified domain belongs to exactly one company; an unverified
            # claim may be duplicated while two companies race to prove it.
            models.UniqueConstraint(
                fields=["domain"],
                condition=models.Q(verified_at__isnull=False),
                name="companydomain_one_owner_when_verified",
            )
        ]

    def __str__(self) -> str:
        return self.domain


class CompanyMembershipQuerySet(models.QuerySet):
    def active(self) -> CompanyMembershipQuerySet:
        return self.filter(deleted_at__isnull=True, status=CompanyMembership.Status.ACTIVE)

    def owners(self) -> CompanyMembershipQuerySet:
        return self.active().filter(role=Role.OWNER)


class CompanyMembership(TimeStampedModel):
    """One person's role inside one company — the authorization spine (ADR-A1)."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        SUSPENDED = "suspended", _("Suspended")

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="company_memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    permissions = models.JSONField(
        default=list,
        blank=True,
        help_text="Extra grant keys layered on the role preset. Validated against the enum.",
    )
    title = models.CharField(max_length=120, blank=True)

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    joined_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = CompanyMembershipQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "user"],
                condition=models.Q(deleted_at__isnull=True),
                name="membership_one_active_per_company_user",
            )
        ]
        indexes = [
            # The company switcher renders on every employer request.
            models.Index(
                fields=["user"],
                condition=models.Q(deleted_at__isnull=True),
                name="membership_by_user_idx",
            ),
            models.Index(fields=["company", "role"], name="membership_company_role_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.company_id} ({self.role})"

    def save(self, *args, **kwargs):
        validate_grants(self.permissions or [])
        return super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return (
            self.deleted_at is None and self.status == self.Status.ACTIVE and self.company.is_active
        )

    @property
    def grants(self) -> frozenset[str]:
        return grants_for(self.role, self.permissions or [])


class OwnershipTransfer(TimeStampedModel):
    """A nomination to take over a company (SRS §2.6 two-party transfer).

    "The current owner nominates, the nominee accepts, both are notified, and
    the transfer is audited." Ownership used to move on one click by the owner
    alone; the nominee had no say in taking on billing and legal control.
    """

    class State(models.TextChoices):
        PENDING = "pending", _("Waiting for an answer")
        ACCEPTED = "accepted", _("Accepted")
        DECLINED = "declined", _("Declined")
        CANCELLED = "cancelled", _("Cancelled")

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="ownership_transfers"
    )
    nominated_by = models.ForeignKey(CompanyMembership, on_delete=models.CASCADE, related_name="+")
    nominee = models.ForeignKey(CompanyMembership, on_delete=models.CASCADE, related_name="+")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # One open nomination per company: two pending nominations could
            # both be accepted and leave the company with owners nobody chose.
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(state="pending"),
                name="ownershiptransfer_one_pending_per_company",
            )
        ]

    def __str__(self) -> str:
        return f"{self.company_id}: {self.nominated_by_id} -> {self.nominee_id} ({self.state})"


class CompanyInvite(TimeStampedModel):
    """An invitation bound to an email address (SRS §2.6, decision D-03).

    Bound to the address, never to whoever happens to be signed in. An account
    accepts only if its main address, or a secondary address it has verified,
    is the invited one; anybody else signs in as that address or asks for the
    invitation to be sent again. There is deliberately no "bind to my current
    account" path — a forwarded link would otherwise put anyone on the team.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="invites")
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=Role.choices)
    permissions = models.JSONField(default=list, blank=True)

    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="invite_one_open_per_company_email",
            )
        ]

    def __str__(self) -> str:
        return f"{self.email} -> {self.company_id}"

    @property
    def is_open(self) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at > timezone.now()
        )


class CompanyVerification(TimeStampedModel):
    """Verification as a state machine, because the middle states are the point.

    A product that only knows "verified / not" has nothing to show an employer
    on the day they are waiting, which is the day they look hardest.
    """

    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="verification")
    state = models.CharField(
        max_length=20, choices=VerificationState.choices, default=VerificationState.NOT_SUBMITTED
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    #: Who sent the current submission. A reviewer may not decide a
    #: submission they sent themselves (decision D-04).
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: Which verification attempt this is: 1 for the first submission, one more
    #: for each new submission after a rejection (decision D-02). Answering a
    #: request for more information is the same attempt. 0 = never submitted.
    attempt = models.PositiveSmallIntegerField(default=0)

    # Claim-and-lock, so two staff reviewers cannot collide on one queue item.
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    claimed_until = models.DateTimeField(null=True, blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"{self.company_id}: {self.state}"

    @property
    def is_claimed(self) -> bool:
        return self.claimed_until is not None and self.claimed_until > timezone.now()


class CompanyVerificationDecision(models.Model):
    """One reviewer decision, kept for good (decision D-02).

    `CompanyVerification` holds the current state and is rewritten by the next
    submission; this row is written once, when a reviewer decides, and never
    changed or deleted. It is what keeps a rejection's reason, reviewer and
    time on record after the company applies again, and what tells a reviewer
    which attempt they are looking at.
    """

    verification = models.ForeignKey(
        CompanyVerification, on_delete=models.CASCADE, related_name="decisions"
    )
    attempt = models.PositiveSmallIntegerField()
    state = models.CharField(max_length=20, choices=VerificationState.choices)
    reason = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: The documents the reviewer decided on: kind, filename, attachment id.
    documents = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ("-decided_at", "-id")

    def __str__(self) -> str:
        return f"{self.verification_id} attempt {self.attempt}: {self.state}"


#: The documents a company is asked for, in the order the screen asks.
VERIFICATION_DOCUMENT_KINDS: dict[str, str] = {
    "registration": "Company registration certificate",
    "pan_vat": "PAN or VAT certificate",
}


class CompanyVerificationDocument(models.Model):
    verification = models.ForeignKey(
        CompanyVerification, on_delete=models.CASCADE, related_name="documents"
    )
    kind = models.CharField(max_length=40)
    attachment = models.ForeignKey("files.Attachment", on_delete=models.PROTECT, related_name="+")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.kind} ({self.verification_id})"

    @property
    def label(self) -> str:
        return VERIFICATION_DOCUMENT_KINDS.get(self.kind, self.kind)


class CompanyFollow(models.Model):
    """One person following one company, to hear when it posts a job.

    A row per relationship rather than a flag or a list: "who follows this
    company" is the query the notification fan-out runs, chunked by primary
    key (ADR-A25), and "does this person follow it" is the query the company
    page runs on every view — both want an index, neither wants a scan of a
    JSON column.

    Deleting the row is unfollowing. Nothing else refers to it, and notices
    already delivered stay where they are: they were true when they were sent.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="company_follows"
    )
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="follows")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # Following twice is following once. Enforced by the database,
            # because two tabs pressing "Follow" is the normal race.
            models.UniqueConstraint(fields=["user", "company"], name="companyfollow_once"),
        ]
        indexes = [
            # The fan-out: every follower of one company, in pk order.
            models.Index(fields=["company", "id"], name="companyfollow_company_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} follows {self.company_id}"


class CompanyUpdateKind(models.TextChoices):
    ANNOUNCEMENT = "announcement", _("Announcement")
    EVENT = "event", _("Event")


class CompanyUpdate(PublicIdModel, TimeStampedModel):
    """Something a company tells the people who follow it (FOL-03).

    An announcement is news; an event has a time and, usually, a place — a
    hiring day, an open office, a webinar. Both appear on the public company
    page and reach followers through the notification system under
    "Companies you follow", the same way a new posting does.

    Withdrawn rather than deleted: a notice already delivered links here, and
    a link that 404s reads as the platform losing things. A withdrawn update
    leaves the public page and its link says it was withdrawn.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="updates")
    kind = models.CharField(max_length=16, choices=CompanyUpdateKind.choices)
    title = models.CharField(max_length=160)
    body = models.TextField(max_length=2000, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=160, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["company", "-created_at"], name="companyupdate_company_idx")
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(kind="event") | models.Q(starts_at__isnull=False),
                name="companyupdate_event_has_time",
            )
        ]

    def __str__(self) -> str:
        return f"{self.company_id}: {self.title}"

    @property
    def is_live(self) -> bool:
        return self.withdrawn_at is None
