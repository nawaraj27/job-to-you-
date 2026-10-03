"""
Delivery: fan-out, ledger, feed, email, webhooks (SRS §5.12, §13.4, ADR-A25).

Two-stage fan-out is the whole design, and it exists for one number: a
`job.published` in a popular category can match fifty thousand candidates.
Single-pass delivery would be one transaction writing a hundred thousand rows,
holding locks, blocking the outbox behind it and restarting from nothing on any
failure.

So the outbox worker resolves the recipient set **as a query, never a list**,
and splits it into `FanoutJob` chunks. Each chunk is small, independently
retryable, and — because `NotificationDelivery` is unique on
`(event, recipient, channel)` — physically unable to double-send.

Guarantees: at-least-once delivery, exactly-once user-visible effect, nothing
sent for a rolled-back transaction, and a provider outage that delays rather
than loses.
"""

from __future__ import annotations

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.crypto import EncryptedCharField
from apps.common.models import PublicIdModel, TimeStampedModel


class Channel(models.TextChoices):
    IN_APP = "in_app", _("In-app")
    EMAIL = "email", _("Email")
    PUSH = "push", _("Push")
    WEBHOOK = "webhook", _("Webhook")


class FanoutState(models.TextChoices):
    PENDING = "pending", _("Pending")
    RUNNING = "running", _("Running")
    DONE = "done", _("Done")
    FAILED = "failed", _("Failed")


class FanoutJobQuerySet(models.QuerySet):
    def claimable(self) -> FanoutJobQuerySet:
        return self.filter(
            state__in=[FanoutState.PENDING, FanoutState.FAILED],
            available_at__lte=timezone.now(),
        )


class FanoutJob(models.Model):
    """One chunk of one event's recipient set (ADR-A25, stage two).

    The cursor is a primary-key range rather than an offset: an offset shifts
    under concurrent inserts, and the symptom is a recipient who is skipped or
    served twice — the second of which the ledger catches and the first of
    which nothing does.
    """

    event = models.ForeignKey(
        "notifications.OutboxEvent", on_delete=models.CASCADE, related_name="fanout_jobs"
    )
    #: Which population this chunk walks. A key rather than a queryset, because
    #: a chunk is retried in a different process from the one that planned it.
    audience = models.CharField(max_length=60)
    cursor_start = models.BigIntegerField()
    cursor_end = models.BigIntegerField()
    chunk_size = models.PositiveIntegerField(default=500)

    state = models.CharField(
        max_length=16, choices=FanoutState.choices, default=FanoutState.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    delivered = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = FanoutJobQuerySet.as_manager()

    class Meta:
        ordering = ("pk",)
        indexes = [
            models.Index(
                fields=["state", "available_at"],
                condition=models.Q(state__in=["pending", "failed"]),
                name="fanoutjob_claimable_idx",
            ),
            models.Index(fields=["event"], name="fanoutjob_event_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.audience}[{self.cursor_start}:{self.cursor_end}] for {self.event_id}"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SENT = "sent", _("Sent")
    SUPPRESSED = "suppressed", _("Suppressed")
    MUTED = "muted", _("Muted by preference")
    FAILED = "failed", _("Failed")


class NotificationDelivery(models.Model):
    """The idempotency ledger (ADR-A25).

    One row per (event, recipient, channel), enforced by a unique constraint.
    A retried chunk cannot double-send because the second insert loses to the
    index — which is a guarantee, unlike "the worker checks first", which is a
    race.

    `MUTED` and `SUPPRESSED` are recorded rather than skipped silently: "why
    did I not get that" is a real support question, and this table is the only
    thing that can answer it.
    """

    event = models.ForeignKey(
        "notifications.OutboxEvent", on_delete=models.CASCADE, related_name="deliveries"
    )
    recipient = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    #: For a webhook there is no user; the endpoint is the recipient.
    endpoint = models.ForeignKey(
        "notifications.WebhookEndpoint",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    status = models.CharField(
        max_length=16, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    reason = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "recipient", "channel"],
                condition=models.Q(recipient__isnull=False),
                name="notificationdelivery_once_per_user",
            ),
            models.UniqueConstraint(
                fields=["event", "endpoint", "channel"],
                condition=models.Q(endpoint__isnull=False),
                name="notificationdelivery_once_per_endpoint",
            ),
        ]
        indexes = [
            models.Index(fields=["event", "channel"], name="notifdelivery_event_idx"),
            models.Index(fields=["recipient", "-created_at"], name="notifdelivery_user_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.channel} to {self.recipient_id or self.endpoint_id}: {self.status}"


class NotificationQuerySet(models.QuerySet):
    def unread(self) -> NotificationQuerySet:
        return self.filter(read_at__isnull=True, dismissed_at__isnull=True)

    def visible(self) -> NotificationQuerySet:
        return self.filter(dismissed_at__isnull=True)

    def for_user(self, user) -> NotificationQuerySet:
        return self.filter(recipient=user)


class Notification(PublicIdModel, TimeStampedModel):
    """One row in somebody's feed.

    `dedupe_key` collapses bursts: forty applications to one posting in an hour
    is one notification that says forty, not forty notifications. The partial
    unique index only covers unread rows, so the next burst after somebody
    reads the last one starts a fresh line rather than being swallowed by it.
    """

    recipient = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="notifications"
    )
    kind = models.CharField(max_length=60)
    #: Which preference group governs it. Stored so the feed can filter on a
    #: muted group without re-deriving it from the event type.
    event_key = models.CharField(max_length=32, blank=True)

    title = models.CharField(max_length=160)
    body = models.CharField(max_length=300, blank=True)
    target_url = models.CharField(max_length=300, blank=True)

    actor_ct = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    actor_id = models.CharField(max_length=64, blank=True)
    actor = GenericForeignKey("actor_ct", "actor_id")

    payload = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True)
    #: How many times this collapsed row has been reinforced.
    count = models.PositiveIntegerField(default=1)

    read_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    objects = NotificationQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=models.Q(dedupe_key__gt="", read_at__isnull=True),
                name="notification_dedupe_while_unread",
            )
        ]
        indexes = [
            models.Index(
                fields=["recipient", "-created_at"],
                condition=models.Q(read_at__isnull=True),
                name="notification_unread_idx",
            ),
            models.Index(fields=["recipient", "kind", "-created_at"], name="notification_kind_idx"),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class EmailStatus(models.TextChoices):
    QUEUED = "queued", _("Queued")
    SENT = "sent", _("Sent")
    FAILED = "failed", _("Failed")
    SUPPRESSED = "suppressed", _("Suppressed")


class EmailMessage(TimeStampedModel):
    """One outbound message, recorded before it is attempted.

    `params` is minimal on purpose (SRS §5.12): the rendered body is not stored
    and neither is a full context dump. Both duplicate personal data into a
    table with a different retention from the row it came from, and both grow
    without bound.
    """

    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    to_email = models.EmailField()
    template_key = models.CharField(max_length=60)
    params = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=16, choices=EmailStatus.choices, default=EmailStatus.QUEUED
    )
    provider_message_id = models.CharField(max_length=200, blank=True)
    error = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["to_email", "-created_at"], name="emailmessage_to_idx"),
            models.Index(
                fields=["status", "-created_at"],
                condition=models.Q(status="queued"),
                name="emailmessage_queued_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.template_key} to {self.to_email}"


class SuppressionReason(models.TextChoices):
    BOUNCE = "bounce", _("Hard bounce")
    COMPLAINT = "complaint", _("Marked as spam")
    MANUAL = "manual", _("Added by staff")
    UNSUBSCRIBED = "unsubscribed", _("Unsubscribed from everything")


class EmailSuppression(models.Model):
    """Addresses this platform will not write to again.

    Consulted before every send. Without it, a bounce storm costs the sending
    domain its reputation, and the cost lands on every other message the
    platform sends — including the ones people are waiting for.
    """

    email = models.EmailField(unique=True)
    reason = models.CharField(max_length=20, choices=SuppressionReason.choices)
    detail = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.email} ({self.reason})"


class WebhookEndpoint(PublicIdModel, TimeStampedModel):
    """Where a company wants events delivered.

    **The secret is encrypted, not hashed** (ADR-A45), and the distinction is
    the whole reason this comment exists. A password is *verified*: the server
    compares a presented value against a hash and never needs the original. A
    webhook secret is *used*: the server computes an HMAC with it on every
    outbound request. Hashing it would leave nothing to sign with — the
    signature header would be a decoration, which is worse than no signature at
    all, because a receiver would be checking something meaningless.

    So it is encrypted at rest with the platform's key, and the plaintext is
    shown once at creation as a convenience — the company can always read it
    back, unlike an API key, because we must be able to as well.
    """

    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="webhook_endpoints"
    )
    url = models.URLField(max_length=500)
    secret = EncryptedCharField(
        max_length=64, help_text="Signing key. Encrypted at rest (ADR-A45)."
    )
    event_types = models.JSONField(default=list, blank=True)
    description = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    #: Consecutive failures. An endpoint that has been dead for a long time is
    #: disabled rather than retried forever.
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["company"],
                condition=models.Q(is_active=True),
                name="webhookendpoint_live_idx",
            )
        ]

    def __str__(self) -> str:
        return self.url

    @property
    def is_live(self) -> bool:
        return self.is_active and self.disabled_at is None

    def wants(self, event_type: str) -> bool:
        """An empty subscription list means everything, which is the useful
        default for a company wiring up their first endpoint."""
        return not self.event_types or event_type in self.event_types


class WebhookDelivery(models.Model):
    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries"
    )
    event = models.ForeignKey(
        "notifications.OutboxEvent", on_delete=models.CASCADE, related_name="webhook_deliveries"
    )
    attempt = models.PositiveSmallIntegerField(default=0)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_excerpt = models.CharField(max_length=500, blank=True)
    error = models.CharField(max_length=300, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["endpoint", "-created_at"], name="webhookdelivery_endpoint_idx"),
            models.Index(
                fields=["next_retry_at"],
                condition=models.Q(delivered_at__isnull=True),
                name="webhookdelivery_retry_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.endpoint_id} attempt {self.attempt}"

    @property
    def succeeded(self) -> bool:
        return self.delivered_at is not None
