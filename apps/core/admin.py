"""Feature flags in admin.

The one thing in this project an operator is expected to change at runtime, so
it is genuinely editable — and every change invalidates the cache immediately,
because a flag that takes thirty seconds to apply is useless during an incident.
"""

from django.contrib import admin

from . import features
from .models import FeatureFlag


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = (
        "key",
        "is_enabled",
        "rollout_percent",
        "staff_only",
        "expires_on",
        "updated_at",
    )
    list_filter = ("is_enabled", "staff_only")
    search_fields = ("key", "notes")
    ordering = ("key",)
    # Set from the signed-in operator on save. Editable, it let a change be
    # attributed to somebody else — and blanked it whenever the form was
    # submitted without it.
    readonly_fields = ("updated_by",)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        features.invalidate_cache()
        _audit(
            "core.feature_flag_changed" if change else "core.feature_flag_created",
            request,
            obj,
            changed=sorted(form.changed_data),
        )

    def delete_model(self, request, obj):
        _audit("core.feature_flag_deleted", request, obj)
        super().delete_model(request, obj)
        features.invalidate_cache()

    def delete_queryset(self, request, queryset):
        # The bulk action deletes without calling `delete_model`, so each row
        # is written here or the bulk path is the unaudited way to do it.
        for flag in queryset:
            _audit("core.feature_flag_deleted", request, flag)
        super().delete_queryset(request, queryset)
        features.invalidate_cache()


def _audit(verb: str, request, flag: FeatureFlag, *, changed: list[str] | None = None) -> None:
    """A flag change is a privileged action (NFR-9), and it is the one an
    operator makes at runtime during an incident — which is exactly when
    "who switched this off, and to what" needs an answer. It had none: the
    admin saved and invalidated the cache and wrote nothing down.
    """
    from apps.audit import services as audit

    audit.record(
        verb=verb,
        actor=request.user,
        target=flag,
        metadata={
            "key": flag.key,
            "is_enabled": flag.is_enabled,
            "rollout_percent": flag.rollout_percent,
            "staff_only": flag.staff_only,
            "expires_on": flag.expires_on.isoformat() if flag.expires_on else None,
            **({"changed": changed} if changed is not None else {}),
        },
    )


from apps.common.admin_tools import RecordAdmin  # noqa: E402

from .models import LegalDocument  # noqa: E402


@admin.register(LegalDocument)
class LegalDocumentAdmin(RecordAdmin):
    """Published terms and policies. A new version is a new row (`core.legal.publish`),
    never an edit: consents name the version a person agreed to."""

    list_display = ("slug", "version", "title", "effective_from", "is_draft")
    list_filter = ("slug", "is_draft")
    search_fields = ("title", "version")
