"""
Custom user model.

Defined up front because swapping AUTH_USER_MODEL after the first migration is
painful, and this platform already knows it has two very different account
types (candidate and employer) plus email-based login.
"""

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel


class UserManager(BaseUserManager):
    """Email is the login identifier; username is not used."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("signup_intent", User.SignupIntent.STAFF)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    class SignupIntent(models.TextChoices):
        """Why someone signed up. A UI hint, never an authority.

        Authorization comes from CompanyMembership and StaffRole (ADR-A1); this
        only chooses an onboarding path and a default landing page. No code may
        branch on it for capability (SRS §2.1).
        """

        CANDIDATE = "candidate", _("Job seeker")
        EMPLOYER = "employer", _("Employer")
        STAFF = "staff", _("Staff")

    # AbstractUser declares these; the email-login pattern replaces both.
    # Documented ignores rather than silence: the override is deliberate.
    username = None  # type: ignore[assignment]
    email = models.EmailField(_("email address"), unique=True)
    full_name = models.CharField(_("full name"), max_length=150, blank=True)
    signup_intent = models.CharField(
        _("account type"),
        max_length=20,
        choices=SignupIntent.choices,
        default=SignupIntent.CANDIDATE,
        help_text="Onboarding path only. Never consulted for authorization.",
    )

    # SRS §5.2. On the user rather than only on `UserPreferences` because email
    # and scheduled jobs have no request to read a preference row from, and a
    # digest sent at 07:00 UTC to someone in Kathmandu arrives at lunchtime.
    locale = models.CharField(max_length=10, default="en")
    timezone = models.CharField(max_length=64, default="Asia/Kathmandu")

    # Erasure (SRS §19.4). Three timestamps rather than a state column, because
    # each answers a different question and the answers are not ordered the way
    # a state machine would suggest: `requested` is cancellable, `started` is
    # the gate every AI task checks on entry and is the point of no return, and
    # `erased` is the permanent record that this row is a tombstone.
    erasure_requested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When erasure was asked for. Cancellable until the job runs.",
    )
    erasure_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set before the first row is removed. Every AI task aborts when this is set.",
    )
    erased_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()  # type: ignore[misc,assignment]

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self) -> str:
        return self.email

    def get_full_name(self) -> str:
        return self.full_name or self.email.split("@")[0]

    def get_short_name(self) -> str:
        return self.get_full_name().split(" ")[0]

    @property
    def initials(self) -> str:
        parts = [p for p in self.get_full_name().replace(".", " ").split(" ") if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def signed_up_to_hire(self) -> bool:
        """Whether to land this user on the employer side after sign-in.

        Deliberately not named `is_employer`: that reads like a capability, and
        the answer to "may this person act for a company" is a membership
        lookup, never this field.
        """
        return self.signup_intent == self.SignupIntent.EMPLOYER


class IdempotencyKey(models.Model):
    """A claimed idempotency key (SRS §5.2, DEVELOPER.md §3.8).

    Any endpoint that creates a record accepts an Idempotency-Key so a retried
    request cannot produce a second record. The row is the claim; `response_ref`
    is the recorded outcome, absent while the operation is still in flight.

    The mechanism lives in apps.common.idempotency — this is only the table.
    """

    key = models.CharField(max_length=128)
    endpoint = models.CharField(
        max_length=200,
        help_text="Scopes the key, so the same client key on two endpoints does not collide.",
    )
    request_hash = models.CharField(
        max_length=64,
        help_text="Fingerprint of the request body. A mismatch means the key was reused.",
    )
    user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="idempotency_keys",
    )
    response_ref = models.JSONField(
        null=True,
        blank=True,
        help_text="Recorded outcome. Null while the first attempt is still running.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = _("idempotency key")
        constraints = [
            models.UniqueConstraint(
                fields=["key", "endpoint"], name="idempotencykey_unique_per_endpoint"
            )
        ]
        indexes = [
            models.Index(fields=["expires_at"], name="idempotencykey_expiry_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.endpoint}:{self.key}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()


class EmailAddress(TimeStampedModel):
    """An address belonging to a user (SRS §5.2).

    Separate from `User.email` because verification is per-address and because
    work-email proof for company domain verification needs a second address
    without disturbing the login identifier.
    """

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="email_addresses"
    )
    email = models.EmailField(unique=True)
    is_primary = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_primary=True),
                name="emailaddress_one_primary_per_user",
            )
        ]

    def __str__(self) -> str:
        return self.email

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None


class StaffRole(TimeStampedModel):
    """Platform authority, separate from company membership (SRS §2.1).

    Global operator power is rare, MFA-mandatory and fully audited. It is not a
    company role and must never be reachable through one.
    """

    class Level(models.TextChoices):
        SUPPORT = "support", _("Support")
        MODERATOR = "moderator", _("Moderator")
        ADMIN = "admin", _("Platform admin")
        SUPERADMIN = "superadmin", _("Super admin")

    user = models.OneToOneField(
        "accounts.User", on_delete=models.CASCADE, related_name="staff_role"
    )
    level = models.CharField(max_length=20, choices=Level.choices)
    granted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    granted_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.user_id}: {self.level}"


class StaffRoleRequest(TimeStampedModel):
    """A proposed staff role, waiting for a second superadmin (SRS §2.3).

    "Granting a staff role requires approval by a second superadmin." The grant
    is a request until somebody other than its proposer approves it, so no one
    person can hand out platform authority — including to themselves.
    """

    class State(models.TextChoices):
        PENDING = "pending", _("Waiting for approval")
        APPROVED = "approved", _("Approved")
        DECLINED = "declined", _("Declined")
        WITHDRAWN = "withdrawn", _("Withdrawn")

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="staff_role_requests"
    )
    level = models.CharField(max_length=20, choices=StaffRole.Level.choices)
    reason = models.CharField(max_length=300)
    requested_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    decided_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # One open proposal per person, so two superadmins cannot each
            # propose a different level and approve each other's.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(state="pending"),
                name="staffrolerequest_one_pending_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id} -> {self.level} ({self.state})"


class UserSession(models.Model):
    """A signed-in device, so a user can see and revoke their sessions.

    A mirror of the session store rather than its source of truth: the store
    decides whether a session is valid, and this records enough to describe it
    to its owner.
    """

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="sessions")
    session_key = models.CharField(max_length=40, unique=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-last_active_at"], name="usersession_user_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.session_key[:8]}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class Consent(models.Model):
    """Every consent in the system, in one table (ADR-A33).

    One shape — subject, purpose, policy version, timestamp, evidence — because
    a regulator asks one question and three tables answer it three ways. Current
    state is the newest row per (user, purpose); nothing is ever updated, so the
    history of what someone agreed to is intact.
    """

    class Purpose(models.TextChoices):
        TERMS = "terms", _("Terms of service")
        PRIVACY = "privacy", _("Privacy policy")
        AI_PROCESSING = "ai_processing", _("AI processing of my documents")
        AI_TRAINING = "ai_training", _("Use of my data to improve models")
        INTERVIEW_TRANSCRIPT = "interview_transcript", _("Interview transcript")
        INTERVIEW_AUDIO = "interview_audio", _("Interview audio")
        INTERVIEW_VIDEO = "interview_video", _("Interview video")
        MARKETING = "marketing", _("Marketing email")

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="consents")
    purpose = models.CharField(max_length=32, choices=Purpose.choices)
    policy_version = models.CharField(max_length=20)
    granted = models.BooleanField()
    granted_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "purpose", "-granted_at"], name="consent_current_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.purpose}={self.granted}"


class UserPreferences(TimeStampedModel):
    """How this account wants the product to look and speak to it.

    On `accounts` rather than on the candidate profile because an employer has
    the same settings screen and no profile, and because these outlive any one
    role: an account that stops being a candidate does not stop preferring
    reduced motion.

    Everything here is read by something. A preference nothing consults is a
    checkbox that lies to the person who ticked it — accessibility switches are
    rendered onto `<html>` before the first paint, theme drives the same
    attribute, locale and timezone format every date the account is shown.
    """

    class Theme(models.TextChoices):
        SYSTEM = "system", _("Match my system")
        LIGHT = "light", _("Light")
        DARK = "dark", _("Dark")

    user = models.OneToOneField(
        "accounts.User", on_delete=models.CASCADE, related_name="preferences"
    )

    theme = models.CharField(max_length=8, choices=Theme.choices, default=Theme.SYSTEM)
    reduce_motion = models.BooleanField(default=False)
    larger_text = models.BooleanField(default=False)
    underline_links = models.BooleanField(default=False)
    high_contrast = models.BooleanField(default=False)

    locale = models.CharField(
        max_length=10,
        default="en",
        help_text="Interface language. Only languages the interface actually has.",
    )
    timezone = models.CharField(max_length=64, default="Asia/Kathmandu")

    share_analytics = models.BooleanField(
        default=True,
        help_text="Product analytics. Separate from anything that identifies the user to a company.",
    )

    class Meta:
        verbose_name_plural = "user preferences"

    def __str__(self) -> str:
        return f"Preferences of {self.user_id}"
