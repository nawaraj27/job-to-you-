"""
Transactional outbox (SRS §13.4, ADR-A14).

A state change and the world being told about it must never disagree. Writing
the event in the same transaction as the change is the only way to guarantee
that using the database alone: if the transaction rolls back, the event was
never there; if it commits, the event is there waiting.

Two tables:

  OutboxEvent       the queue. Short-lived — processed rows are deleted after a
                    grace period, because this is not a history. `AuditLog` and
                    the domain timelines are the history (SRS §5.15).

  OutboxConsumerRun the ledger that makes at-least-once delivery produce an
                    exactly-once effect. A unique constraint on
                    (event, consumer) means a retried batch physically cannot
                    run the same consumer twice for the same event.
"""

from __future__ import annotations

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone


class OutboxEventQuerySet(models.QuerySet):
    def pending(self) -> OutboxEventQuerySet:
        return self.filter(
            processed_at__isnull=True,
            dead_lettered_at__isnull=True,
            available_at__lte=timezone.now(),
        )

    def dead_lettered(self) -> OutboxEventQuerySet:
        return self.filter(dead_lettered_at__isnull=False)


class OutboxEvent(models.Model):
    type = models.CharField(max_length=100, db_index=True)
    schema_version = models.PositiveSmallIntegerField(default=1)

    aggregate_ct = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.SET_NULL)
    aggregate_id = models.CharField(max_length=64, blank=True)
    aggregate = GenericForeignKey("aggregate_ct", "aggregate_id")

    payload = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)
    available_at = models.DateTimeField(
        default=timezone.now,
        help_text="Not dispatched before this time. Moved forward by retry backoff.",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)

    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    objects = OutboxEventQuerySet.as_manager()

    class Meta:
        indexes = [
            # The drain query: unprocessed, due, oldest first.
            models.Index(
                fields=["available_at"],
                name="outbox_pending_idx",
                condition=models.Q(processed_at__isnull=True, dead_lettered_at__isnull=True),
            ),
            models.Index(
                fields=["processed_at"],
                name="outbox_processed_idx",
                condition=models.Q(processed_at__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.type}#{self.pk}"


class OutboxConsumerRun(models.Model):
    """One row per (event, consumer). The delivery ledger."""

    class Status(models.TextChoices):
        OK = "ok", "Delivered"
        FAILED = "failed", "Failed"

    event = models.ForeignKey(OutboxEvent, on_delete=models.CASCADE, related_name="consumer_runs")
    consumer = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=Status.choices)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "consumer"], name="outbox_run_once_per_consumer"
            )
        ]

    def __str__(self) -> str:
        return f"{self.consumer} <- {self.event_id} ({self.status})"


class NotificationPreference(models.Model):
    """One row per (user, event group, channel set) — SRS §5.14, §14.1.

    Seven groups, two channels, and **every notification kind maps to exactly one
    group**. A kind with no group is a message the settings screen cannot
    describe, which means a user who has switched everything off still gets it
    and has no way to find out why.

    Delivery arrives in P11. The rows exist now because the settings screen is
    P3 work and a screen writing to a table nothing reads yet is still honest —
    a screen writing to a session that P11 will silently discard is not.
    """

    class Event(models.TextChoices):
        JOB_MATCHES = "job_matches", "New job matches"
        APPLICATION_UPDATES = "application_updates", "Application updates"
        EMPLOYER_MESSAGES = "employer_messages", "Messages from employers"
        CV_INSIGHTS = "cv_insights", "CV insights"
        WEEKLY_DIGEST = "weekly_digest", "Weekly digest"
        PRODUCT_NEWS = "product_news", "Product news"
        COMPANY_UPDATES = "company_updates", "Companies you follow"

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="notification_preferences"
    )
    event_key = models.CharField(max_length=32, choices=Event.choices)
    in_app = models.BooleanField(default=True)
    email = models.BooleanField(default=True)
    push = models.BooleanField(default=False)
    quiet_hours = models.JSONField(
        default=dict,
        blank=True,
        help_text='e.g. {"from": "22:00", "to": "07:00", "tz": "Asia/Kathmandu"}',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event_key"], name="notificationpreference_once_per_event"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.event_key}"

    @property
    def is_silent(self) -> bool:
        return not (self.in_app or self.email or self.push)


# --- delivery (P10) -------------------------------------------------------
#
# Split into `delivery_models.py` because the fan-out, ledger, feed, email and
# webhook tables are one subject and this module was already the outbox's. Both
# halves are re-exported here so `from apps.notifications.models import X`
# keeps working for every caller, and Django sees one app label either way.

from .delivery_models import (  # noqa: E402
    Channel,
    DeliveryStatus,
    EmailMessage,
    EmailStatus,
    EmailSuppression,
    FanoutJob,
    FanoutState,
    Notification,
    NotificationDelivery,
    NotificationQuerySet,
    SuppressionReason,
    WebhookDelivery,
    WebhookEndpoint,
)

__all__ = [
    "Channel",
    "DeliveryStatus",
    "EmailMessage",
    "EmailStatus",
    "EmailSuppression",
    "FanoutJob",
    "FanoutState",
    "Notification",
    "NotificationDelivery",
    "NotificationPreference",
    "NotificationQuerySet",
    "OutboxConsumerRun",
    "OutboxEvent",
    "SuppressionReason",
    "WebhookDelivery",
    "WebhookEndpoint",
]
