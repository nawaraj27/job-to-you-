"""
The delivery machinery in the admin (docs/ADMIN.md).

What an operator needs when "I never got the email" arrives: was the event
written, was it fanned out, did a delivery go, what did the provider say. All
read-only. Payloads, notification bodies and email parameters are left out:
they carry names, links and sometimes sign-in tokens. A webhook's signing
secret is never shown.
"""

from __future__ import annotations

from django.contrib import admin

from apps.common.admin_tools import RecordAdmin

from .models import (
    EmailMessage,
    EmailSuppression,
    FanoutJob,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    OutboxConsumerRun,
    OutboxEvent,
    WebhookDelivery,
    WebhookEndpoint,
)


@admin.register(OutboxEvent)
class OutboxEventAdmin(RecordAdmin):
    private_fields = ("payload",)
    list_display = (
        "id",
        "type",
        "aggregate_ct",
        "aggregate_id",
        "created_at",
        "processed_at",
        "dead_lettered_at",
        "attempts",
    )
    list_filter = ("type", "dead_lettered_at")
    search_fields = ("type", "aggregate_id")
    date_hierarchy = "created_at"


@admin.register(OutboxConsumerRun)
class OutboxConsumerRunAdmin(RecordAdmin):
    list_display = ("event", "consumer", "status", "created_at")
    list_filter = ("consumer", "status")


@admin.register(FanoutJob)
class FanoutJobAdmin(RecordAdmin):
    list_display = (
        "event",
        "audience",
        "state",
        "attempts",
        "delivered",
        "created_at",
        "completed_at",
    )
    list_filter = ("state", "audience")


@admin.register(Notification)
class NotificationAdmin(RecordAdmin):
    private_fields = ("title", "body", "payload", "target_url", "dedupe_key")
    safe_name = True
    list_display = ("public_id", "recipient", "kind", "event_key", "count", "created_at", "read_at")
    list_filter = ("kind",)
    search_fields = ("recipient__email", "event_key")
    date_hierarchy = "created_at"


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(RecordAdmin):
    list_display = ("event", "recipient", "channel", "status", "reason", "created_at")
    list_filter = ("channel", "status")
    search_fields = ("recipient__email",)


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(RecordAdmin):
    list_display = ("user", "event_key", "in_app", "email", "push")
    search_fields = ("user__email", "event_key")


@admin.register(EmailMessage)
class EmailMessageAdmin(RecordAdmin):
    private_fields = ("params",)
    list_display = ("to_email", "template_key", "status", "sent_at", "opened_at", "created_at")
    list_filter = ("status", "template_key")
    search_fields = ("to_email", "provider_message_id")
    date_hierarchy = "created_at"


@admin.register(EmailSuppression)
class EmailSuppressionAdmin(RecordAdmin):
    list_display = ("email", "reason", "created_at")
    list_filter = ("reason",)
    search_fields = ("email",)


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(RecordAdmin):
    private_fields = ("secret",)
    list_display = ("url", "company", "is_active", "consecutive_failures", "disabled_at")
    list_filter = ("is_active",)
    search_fields = ("url", "company__name")


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(RecordAdmin):
    private_fields = ("response_excerpt",)
    list_display = ("endpoint", "event", "attempt", "status_code", "delivered_at", "next_retry_at")
    list_filter = ("status_code",)
