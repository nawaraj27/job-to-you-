from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "signup_intent", "is_active", "date_joined")
    list_filter = ("signup_intent", "is_active", "is_staff")
    search_fields = ("email", "full_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "signup_intent")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "signup_intent", "password1", "password2"),
            },
        ),
    )


# --- records (admin inventory, docs/ADMIN.md) -------------------------------

from django_otp.plugins.otp_static.models import StaticDevice  # noqa: E402
from django_otp.plugins.otp_totp.models import TOTPDevice  # noqa: E402

from apps.common.admin_tools import RecordAdmin  # noqa: E402

from .models import (  # noqa: E402
    Consent,
    EmailAddress,
    IdempotencyKey,
    StaffRole,
    StaffRoleRequest,
    UserPreferences,
    UserSession,
)

# A second factor is a credential. View-only, and its secret never shown.
for _device in (TOTPDevice, StaticDevice):
    if admin.site.is_registered(_device):
        admin.site.unregister(_device)


@admin.register(TOTPDevice)
class TOTPDeviceRecordAdmin(RecordAdmin):
    private_fields = ("key",)
    list_display = ("user", "name", "confirmed", "created_at", "last_used_at")
    list_filter = ("confirmed",)
    search_fields = ("user__email",)


@admin.register(StaticDevice)
class StaticDeviceRecordAdmin(RecordAdmin):
    list_display = ("user", "name", "confirmed", "created_at", "last_used_at")
    search_fields = ("user__email",)


@admin.register(Consent)
class ConsentAdmin(RecordAdmin):
    list_display = ("user", "purpose", "policy_version", "granted", "granted_at", "revoked_at")
    list_filter = ("purpose", "granted")
    search_fields = ("user__email",)
    date_hierarchy = "granted_at"


@admin.register(EmailAddress)
class EmailAddressAdmin(RecordAdmin):
    list_display = ("email", "user", "is_primary", "verified_at")
    list_filter = ("is_primary",)
    search_fields = ("email", "user__email")


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(RecordAdmin):
    private_fields = ("key", "request_hash", "response_ref")
    list_display = ("endpoint", "user", "created_at", "expires_at")
    search_fields = ("endpoint",)


@admin.register(StaffRole)
class StaffRoleAdmin(RecordAdmin):
    """Granted and revoked in the staff portal, which audits it (SRS §2.4)."""

    list_display = ("user", "level", "granted_by", "granted_at")
    list_filter = ("level",)
    search_fields = ("user__email",)


@admin.register(StaffRoleRequest)
class StaffRoleRequestAdmin(RecordAdmin):
    list_display = ("user", "level", "state", "requested_by", "decided_by", "decided_at")
    list_filter = ("state", "level")
    search_fields = ("user__email",)


@admin.register(UserPreferences)
class UserPreferencesAdmin(RecordAdmin):
    list_display = ("user", "theme", "locale", "timezone", "share_analytics")
    list_filter = ("theme", "share_analytics")
    search_fields = ("user__email",)


@admin.register(UserSession)
class UserSessionAdmin(RecordAdmin):
    """Which devices are signed in. The session key is a bearer credential."""

    private_fields = ("session_key",)
    safe_name = True
    list_display = ("user", "ip", "created_at", "last_active_at", "revoked_at")
    search_fields = ("user__email",)
    date_hierarchy = "created_at"
