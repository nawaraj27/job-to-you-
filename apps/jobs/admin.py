"""
Jobs in Django admin (ADR-A5).

Read-only for postings: a job's lifecycle is a state machine with permissions
and events attached, and a staff member flipping `status` in a change form
would skip all three. Staff who need to intervene suspend the company, which is
a company-level decision with its own service.
"""

from __future__ import annotations

from django.contrib import admin

from .models import Job, JobAlertMatch, JobAssignment, JobScreeningQuestion, JobSkill, JobView


class JobSkillInline(admin.TabularInline):
    model = JobSkill
    extra = 0
    fields = ("skill", "requirement", "weight", "min_years")
    autocomplete_fields = ("skill",)
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


class JobAssignmentInline(admin.TabularInline):
    model = JobAssignment
    extra = 0
    fields = ("membership", "kind")
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "company",
        "status",
        "workplace",
        "remote_scope",
        "published_at",
        "closes_at",
        "views_count",
        "applications_count",
    )
    list_filter = ("status", "workplace", "remote_scope", "employment_type", "experience_level")
    search_fields = ("title", "slug", "public_id", "company__name")
    date_hierarchy = "published_at"
    inlines = [JobSkillInline, JobAssignmentInline]
    readonly_fields = tuple(field.name for field in Job._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # P8's applications point at postings; deleting one orphans them.
        return False


@admin.register(JobScreeningQuestion)
class JobScreeningQuestionAdmin(admin.ModelAdmin):
    list_display = ("job", "order", "kind", "text", "is_required")
    list_filter = ("kind", "is_required")
    search_fields = ("text", "job__title")
    readonly_fields = tuple(field.name for field in JobScreeningQuestion._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(JobView)
class JobViewAdmin(admin.ModelAdmin):
    """Counts only. `viewer_hash` is here so a support question about a
    suspicious spike has something to look at; it identifies nobody."""

    list_display = ("job", "created_at", "referrer")
    list_filter = ("created_at",)
    search_fields = ("job__title",)
    readonly_fields = tuple(field.name for field in JobView._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(JobAlertMatch)
class JobAlertMatchAdmin(admin.ModelAdmin):
    list_display = ("job", "saved_search", "score", "computed_at", "notified_at")
    list_filter = ("computed_at",)
    readonly_fields = tuple(field.name for field in JobAlertMatch._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
