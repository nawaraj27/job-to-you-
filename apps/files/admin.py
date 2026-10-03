"""Attachments in admin: inspectable, not editable, never downloadable.

Decision D-01: Django's `is_staff` governs the admin site and nothing else. The
admin shows a file's metadata (name, type, scan state) and has no link, field
or action that serves its bytes; private files are read only through the
StaffRole-gated, audited staff screens (`portals.staff`), never from here.
Deleting a row here would also bypass the soft delete and its audit, so that
is refused too.
"""

from django.contrib import admin

from .models import Attachment, AttachmentAccess


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("filename", "mime", "size", "visibility", "scan_status", "created_at")
    list_filter = ("visibility", "scan_status", "mime")
    search_fields = ("filename", "sha256", "public_id")
    readonly_fields = tuple(field.name for field in Attachment._meta.fields)
    ordering = ("-created_at",)

    def has_add_permission(self, request) -> bool:
        # Files enter through apps.files.services.store, which validates,
        # hashes and queues a scan. An admin upload would bypass all three.
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # `files.services.soft_delete` is the only way a file goes, audited.
        return False


@admin.register(AttachmentAccess)
class AttachmentAccessAdmin(admin.ModelAdmin):
    list_display = ("created_at", "attachment", "actor", "purpose", "ip")
    list_filter = ("purpose", "created_at")
    search_fields = ("attachment__filename", "actor__email", "request_id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("attachment", "actor")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


from apps.common.admin_tools import RecordAdmin  # noqa: E402

from .models import AttachmentGrant  # noqa: E402


@admin.register(AttachmentGrant)
class AttachmentGrantAdmin(RecordAdmin):
    """Who was given access to which file, and until when. Never the file."""

    list_display = ("attachment", "user", "reason", "granted_at", "expires_at", "revoked_at")
    search_fields = ("user__email",)
    date_hierarchy = "granted_at"
