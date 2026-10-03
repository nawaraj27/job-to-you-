"""Shared records (docs/ADMIN.md)."""

from __future__ import annotations

from django.contrib import admin

from .admin_tools import RecordAdmin
from .models import ConsumedToken


@admin.register(ConsumedToken)
class ConsumedTokenAdmin(RecordAdmin):
    """Single-use tokens already spent. The id is a hash, never the token."""

    list_display = ("purpose", "consumed_at", "expires_at")
    list_filter = ("purpose",)
    date_hierarchy = "consumed_at"
