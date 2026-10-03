"""
Audit log in Django admin — read-only, always.

Staff need to read the audit trail; nobody may write it by hand. Django admin
has no object-level permission story worth trusting at this data sensitivity
(ADR-A5), so every mutating capability is removed rather than permission-gated.
"""

from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "verb", "actor", "target_repr", "request_id")
    list_filter = ("verb", "created_at")
    search_fields = ("verb", "target_repr", "request_id", "actor__email")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("actor", "on_behalf_of")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
