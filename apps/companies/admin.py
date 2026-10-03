"""
Companies in the admin (docs/ADMIN.md).

Read-only. Verification, moderation, membership and ownership changes are
staff-portal actions that audit and emit events; a form here would bypass both.
Verification documents are not in the admin at all; they are reached only
through the staff portal's audited verification review (decision D-01).
"""

from __future__ import annotations

from django.contrib import admin

from apps.common.admin_tools import RecordAdmin

from .models import (
    Company,
    CompanyDomain,
    CompanyFollow,
    CompanyInvite,
    CompanyMembership,
    CompanyUpdate,
    CompanyVerification,
    CompanyVerificationDecision,
    OwnershipTransfer,
)


class MembershipInline(admin.TabularInline):
    model = CompanyMembership
    fields = ("user", "role", "status", "joined_at")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Company)
class CompanyAdmin(RecordAdmin):
    list_display = ("name", "slug", "state", "verification_state", "verified_at", "created_at")
    list_filter = ("state", "verification_state", "size_band")
    search_fields = ("name", "slug", "legal_name")
    date_hierarchy = "created_at"
    inlines = (MembershipInline,)


@admin.register(CompanyMembership)
class CompanyMembershipAdmin(RecordAdmin):
    list_display = ("company", "user", "role", "status", "joined_at")
    list_filter = ("role", "status")
    search_fields = ("company__name", "user__email")


@admin.register(CompanyInvite)
class CompanyInviteAdmin(RecordAdmin):
    private_fields = ("token_hash",)
    list_display = (
        "company",
        "email",
        "role",
        "invited_by",
        "expires_at",
        "accepted_at",
        "revoked_at",
    )
    list_filter = ("role",)
    search_fields = ("company__name", "email")


@admin.register(CompanyDomain)
class CompanyDomainAdmin(RecordAdmin):
    list_display = ("domain", "company", "verified_at", "created_at")
    search_fields = ("domain", "company__name")


@admin.register(CompanyFollow)
class CompanyFollowAdmin(RecordAdmin):
    list_display = ("company", "user", "created_at")
    search_fields = ("company__name",)


@admin.register(CompanyUpdate)
class CompanyUpdateAdmin(RecordAdmin):
    """Public posts on a company page."""

    list_display = ("title", "company", "kind", "created_at", "withdrawn_at")
    list_filter = ("kind",)
    search_fields = ("title", "company__name")


@admin.register(CompanyVerification)
class CompanyVerificationAdmin(RecordAdmin):
    list_display = ("company", "state", "attempt", "submitted_at", "reviewed_by", "reviewed_at")
    list_filter = ("state",)
    search_fields = ("company__name",)


@admin.register(CompanyVerificationDecision)
class CompanyVerificationDecisionAdmin(RecordAdmin):
    private_fields = ("documents",)
    list_display = ("verification", "attempt", "state", "reviewed_by", "decided_at")
    list_filter = ("state",)


# `CompanyVerificationDocument` is deliberately absent (decision D-01,
# apps/files/tests/test_admin_privacy.py): the documents are reached only
# through the staff portal's audited verification review.


@admin.register(OwnershipTransfer)
class OwnershipTransferAdmin(RecordAdmin):
    list_display = ("company", "nominated_by", "nominee", "state", "decided_at")
    list_filter = ("state",)
