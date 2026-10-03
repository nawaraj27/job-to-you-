"""
Interviews in Django admin: templates editable, everything else read-only.

The split is the point. A template is configuration a staff member legitimately
edits — a question that turns out to be a bad question should be fixable. A
session, its turns and its evaluations are a record of what happened, and a
record somebody can edit is not one.

Answers are deliberately absent from every changelist. A staff screen that stays
open all day should not be a reader of what candidates said in interviews.
"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    InterviewerAvailability,
    InterviewEvaluation,
    InterviewOutcome,
    InterviewRound,
    InterviewSession,
    InterviewSlot,
    InterviewTemplate,
    InterviewTurn,
)


@admin.register(InterviewTemplate)
class InterviewTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "kind", "rubric_version", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(InterviewRound)
class InterviewRoundAdmin(admin.ModelAdmin):
    list_display = ("name", "job", "order", "template", "is_required")
    list_filter = ("is_required",)
    raw_id_fields = ("job", "template")


@admin.register(InterviewSession)
class InterviewSessionAdmin(admin.ModelAdmin):
    """Read-only. A session is a record of what happened."""

    list_display = ("public_id", "kind", "state", "candidate", "scheduled_at", "created_at")
    list_filter = ("kind", "state")
    search_fields = ("public_id", "room_ref")
    readonly_fields = tuple(field.name for field in InterviewSession._meta.fields)
    raw_id_fields = ("application", "candidate", "template", "round", "rescheduled_from")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(InterviewTurn)
class InterviewTurnAdmin(admin.ModelAdmin):
    """Questions, never answers.

    `answer_text` is not in the list, not searchable and not on the form: what a
    candidate said in an interview is not staff reading material, and the reason
    to open this screen is to diagnose a broken session rather than to read one.
    """

    list_display = ("session", "order", "competency", "question_origin", "answered_at")
    list_filter = ("question_origin",)
    readonly_fields = ("session", "order", "competency", "question_text", "question_origin")
    fields = readonly_fields
    raw_id_fields = ("session",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(InterviewEvaluation)
class InterviewEvaluationAdmin(admin.ModelAdmin):
    list_display = ("session", "competency", "score", "by_ai", "submitted_at")
    list_filter = ("score", "rubric_version")
    readonly_fields = tuple(field.name for field in InterviewEvaluation._meta.fields)
    raw_id_fields = ("session", "evaluator", "ai_run")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(boolean=True, description="AI")
    def by_ai(self, obj: InterviewEvaluation) -> bool:
        return obj.by_ai


@admin.register(InterviewOutcome)
class InterviewOutcomeAdmin(admin.ModelAdmin):
    list_display = ("session", "recommendation", "average_score", "shared_with_candidate")
    list_filter = ("recommendation", "shared_with_candidate")
    readonly_fields = tuple(field.name for field in InterviewOutcome._meta.fields)
    raw_id_fields = ("session", "decided_by", "ai_run")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(InterviewSlot)
class InterviewSlotAdmin(admin.ModelAdmin):
    """Read-only, and deliberately so.

    Editing a slot's times by hand would route around the exclusion constraint's
    error message rather than the constraint itself — the write would still be
    refused, but as a 500 in the admin rather than as a sentence somebody can
    act on. Slots move through `services.scheduling`.
    """

    list_display = ("interviewer", "starts_at", "ends_at", "state", "held_until")
    list_filter = ("state",)
    readonly_fields = tuple(field.name for field in InterviewSlot._meta.fields)
    raw_id_fields = ("interviewer", "session")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(InterviewerAvailability)
class InterviewerAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("membership", "starts_at", "ends_at", "timezone_name")
    raw_id_fields = ("membership",)


from apps.common.admin_tools import RecordAdmin  # noqa: E402

from .models import InterviewParticipant  # noqa: E402


@admin.register(InterviewParticipant)
class InterviewParticipantAdmin(RecordAdmin):
    list_display = ("session", "user", "role", "attended", "invited_at")
    list_filter = ("role", "attended")
