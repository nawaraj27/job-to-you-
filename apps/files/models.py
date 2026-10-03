"""
Attachments (SRS §5.13).

One table for every file the platform holds. Domain models point *at* an
Attachment; Attachment does not point back polymorphically, which keeps quota
accounting, purge-on-erasure and access policy as plain joins.

Owner columns (`owner_candidate`, `owner_company`) are part of the target
schema but not of this phase: the models they reference are built in P3 and P1
respectively. They arrive by the expand pattern (SRS §5.16) — add nullable,
backfill, constrain — rather than being stubbed with a nullable integer that
nothing can validate.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.models import PublicIdModel, TimeStampedModel

from .constants import ScanStatus, Visibility


class AttachmentQuerySet(models.QuerySet):
    def alive(self) -> AttachmentQuerySet:
        return self.filter(deleted_at__isnull=True)

    def servable(self) -> AttachmentQuerySet:
        """Clean, not deleted, not expired: the only files that may be served."""
        return (
            self.alive()
            .filter(scan_status=ScanStatus.CLEAN)
            .exclude(expires_at__lte=timezone.now())
        )

    def expired(self) -> AttachmentQuerySet:
        return self.alive().filter(expires_at__lte=timezone.now())


class Attachment(PublicIdModel, TimeStampedModel):
    storage_key = models.CharField(
        max_length=512,
        help_text="Content-addressed path within its storage backend.",
    )
    filename = models.CharField(max_length=255, help_text="Sanitised original name.")
    mime = models.CharField(max_length=127)
    size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)

    visibility = models.CharField(
        max_length=16, choices=Visibility.choices, default=Visibility.PRIVATE
    )

    scan_status = models.CharField(
        max_length=16, choices=ScanStatus.choices, default=ScanStatus.PENDING
    )
    scan_signature = models.CharField(max_length=255, blank=True)
    scan_error = models.CharField(max_length=255, blank=True)
    scanned_at = models.DateTimeField(null=True, blank=True)

    owner_company = models.ForeignKey(
        "companies.Company",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachments",
        help_text="Set for company-owned files. The candidate owner arrives in P3.",
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_attachments",
    )

    expires_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = AttachmentQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["scan_status"], name="attachment_scan_idx"),
            models.Index(
                fields=["expires_at"],
                name="attachment_expiry_idx",
                condition=models.Q(deleted_at__isnull=True, expires_at__isnull=False),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(size__gte=0), name="attachment_size_non_negative"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.filename} ({self.public_id})"

    @property
    def storage_alias(self) -> str:
        """Which configured backend holds the bytes."""
        return "default" if self.visibility == Visibility.PUBLIC else "private"

    @property
    def is_servable(self) -> bool:
        if self.deleted_at is not None or self.scan_status != ScanStatus.CLEAN:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()


class AttachmentGrant(models.Model):
    """Why one person, who did not upload it, may read one file.

    Private attachments are readable by their uploader alone, which is right
    for a CV and wrong for a message attachment: the person it was sent to has
    to be able to open it. The alternative was for `files.policies` to learn
    about conversations, applications and everything else that might share a
    file — a platform app growing a branch per domain, each one silent when it
    is wrong.

    So access is a row, the same shape as `CandidateVisibilityGrant` (INV-4):
    the domain that shares a file says who may read it and why, and the file
    layer answers "is there a live grant" without knowing what a conversation
    is.
    """

    attachment = models.ForeignKey(Attachment, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attachment_grants"
    )
    reason = models.CharField(
        max_length=60, help_text="Why, in the granting domain's words. e.g. 'message'."
    )
    granted_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["attachment", "user", "reason"], name="attachmentgrant_once_per_reason"
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "attachment"],
                condition=models.Q(revoked_at__isnull=True),
                name="attachmentgrant_live_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id} may read {self.attachment_id} ({self.reason})"

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()


class AttachmentAccess(models.Model):
    """Every read of a non-public file (SRS §5.13).

    Two jobs: it is the source for "who viewed your CV", which is a candidate
    trust feature; and it is the signal for detecting a scraper operating a
    legitimate account, because that behaviour is invisible in application data
    and obvious here.
    """

    attachment = models.ForeignKey(Attachment, on_delete=models.CASCADE, related_name="accesses")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="attachment_accesses",
    )
    purpose = models.CharField(max_length=64, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "attachment accesses"
        indexes = [
            models.Index(fields=["attachment", "-created_at"], name="attachaccess_file_idx"),
            models.Index(fields=["actor", "-created_at"], name="attachaccess_actor_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.attachment_id} read by {self.actor_id or 'anonymous'}"
