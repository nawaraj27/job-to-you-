"""
Messaging in Django admin (ADR-A5).

Conversations and messages are **not registered at all**. A staff list view of
hiring conversations is a surveillance surface, and the moderation work that
genuinely needs to read one has a report attached to it — which is what is
registered here.

Reports are reviewable, because that is the one staff action this domain has.
"""

from __future__ import annotations

from django.contrib import admin

from .models import ConversationReport


@admin.register(ConversationReport)
class ConversationReportAdmin(admin.ModelAdmin):
    """The moderation queue. Read-only: resolving goes through the service, so
    the decision is audited and the reviewer is recorded."""

    list_display = ("conversation", "reason", "state", "created_at", "reviewed_by")
    list_filter = ("state", "reason", "created_at")
    search_fields = ("conversation__company__name", "detail")
    date_hierarchy = "created_at"
    readonly_fields = tuple(field.name for field in ConversationReport._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # A dismissed report is evidence about a company's record.
        return False


# --- records (admin inventory, docs/ADMIN.md) -------------------------------
#
# Conversations between a company and a candidate are private to them. The
# admin shows that a conversation exists and when it moved; reading a message
# is a staff-portal action on a reported conversation, behind a StaffRole and
# an access log (decision D-01).

from apps.common.admin_tools import RecordAdmin  # noqa: E402

from .models import Conversation  # noqa: E402


@admin.register(Conversation)
class ConversationAdmin(RecordAdmin):
    private_fields = ("subject",)
    safe_name = True
    list_display = ("public_id", "company", "candidate", "job", "state", "last_message_at")
    list_filter = ("state",)
    search_fields = ("company__name",)
    date_hierarchy = "created_at"


# `Message` is deliberately absent (decision D-01,
# apps/files/tests/test_admin_privacy.py): message text is read only through the
# staff portal, behind a StaffRole and an access log, and only for a reported
# conversation. The conversation's metadata above is all the admin shows.
