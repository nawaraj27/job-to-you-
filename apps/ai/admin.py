"""
The staff run inspector (SRS §5.11, ADR-A5).

Read-only, and it is the one admin surface in this codebase that genuinely
earns its place: when somebody asks "why did the analysis say that", the answer
is one row — which prompt version, which model, what went in, what came back
before parsing, what it cost and how long it took.

`raw_output` is the field that matters most and the one an ORM-shaped admin
would hide. A schema error is unreadable without it.

Nothing here is editable. Re-running a task is a service call with an actor and
a budget; a staff member editing an `AIRun` would be rewriting the record of
what a model actually said.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from .models import AIFeedback, AIRun, AIUsageQuota, PromptTemplate


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PromptTemplate)
class PromptTemplateAdmin(ReadOnlyAdmin):
    """Loaded from files (ADR-A20). Editable here would defeat the point."""

    list_display = ("task_key", "version", "short_hash", "source_path", "loaded_at")
    list_filter = ("task_key",)
    search_fields = ("task_key", "body_sha256")
    readonly_fields = tuple(field.name for field in PromptTemplate._meta.fields)


class AIFeedbackInline(admin.TabularInline):
    model = AIFeedback
    extra = 0
    readonly_fields = ("user", "verdict", "comment", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(AIRun)
class AIRunAdmin(ReadOnlyAdmin):
    list_display = (
        "created_at",
        "task_key",
        "status",
        "model",
        "cost",
        "latency_ms",
        "cache_marker",
        "user",
    )
    list_filter = ("status", "task_key", "provider", "created_at")
    search_fields = ("public_id", "input_hash", "user__email")
    date_hierarchy = "created_at"
    inlines = [AIFeedbackInline]
    readonly_fields = (
        *(field.name for field in AIRun._meta.fields),
        "pretty_output",
    )

    @admin.display(description="cached")
    def cache_marker(self, obj: AIRun) -> str:
        return "reused" if obj.was_cached else ""

    @admin.display(description="Output")
    def pretty_output(self, obj: AIRun) -> str:
        import json

        return format_html("<pre>{}</pre>", json.dumps(obj.output, indent=2)[:20_000])


@admin.register(AIUsageQuota)
class AIUsageQuotaAdmin(admin.ModelAdmin):
    """The one editable field in this app.

    Raising somebody's ceiling is a support action that happens while they are
    waiting, and routing it through a deploy would mean it does not happen.
    Everything else about what ran stays read-only.
    """

    list_display = ("owner_id", "owner_ct", "period_start", "runs", "cost", "limit_cost")
    list_filter = ("period", "period_start")
    readonly_fields = ("owner_ct", "owner_id", "period", "period_start", "runs", "tokens", "cost")

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(AIFeedback)
class AIFeedbackAdmin(ReadOnlyAdmin):
    """The eval corpus, as it accumulates."""

    list_display = ("created_at", "run", "verdict", "user")
    list_filter = ("verdict", "created_at")
    search_fields = ("comment",)
    readonly_fields = tuple(field.name for field in AIFeedback._meta.fields)
