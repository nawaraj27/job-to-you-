"""
Applications in Django admin (ADR-A5).

Read-only throughout. An application's state is a pipeline with permissions,
events and a candidate-visible timeline attached; a staff member editing
`stage` in a change form would skip all three and the candidate would be told
nothing. Support intervenes through the services, or not at all.

Notes and ratings are not registered at all: they are company-internal
permanently (FR-6.11), and an admin list view is the one place that promise
would quietly break.
"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    Application,
    ApplicationEvent,
    CandidateVisibilityGrant,
    Offer,
    PipelineStage,
    TalentPool,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PipelineStage)
class PipelineStageAdmin(ReadOnlyAdmin):
    list_display = ("label", "company", "key", "kind", "order", "reveals_contact")
    list_filter = ("kind", "reveals_contact", "is_visible_to_candidate")
    search_fields = ("label", "key", "company__name")
    readonly_fields = tuple(field.name for field in PipelineStage._meta.fields)


@admin.register(Application)
class ApplicationAdmin(ReadOnlyAdmin):
    list_display = ("public_id", "job", "candidate", "stage", "applied_at", "closed_at")
    list_filter = ("stage", "source", "applied_at")
    search_fields = ("public_id", "job__title", "candidate__user__email")
    date_hierarchy = "applied_at"
    readonly_fields = tuple(field.name for field in Application._meta.fields)


@admin.register(ApplicationEvent)
class ApplicationEventAdmin(ReadOnlyAdmin):
    """Append-only by construction; read-only here so it stays that way."""

    list_display = ("application", "type", "actor_kind", "visible_to_candidate", "created_at")
    list_filter = ("type", "actor_kind", "visible_to_candidate")
    date_hierarchy = "created_at"
    readonly_fields = tuple(field.name for field in ApplicationEvent._meta.fields)


@admin.register(Offer)
class OfferAdmin(ReadOnlyAdmin):
    list_display = ("application", "state", "salary", "currency", "expires_at", "decided_at")
    list_filter = ("state",)
    readonly_fields = tuple(field.name for field in Offer._meta.fields)


@admin.register(CandidateVisibilityGrant)
class CandidateVisibilityGrantAdmin(ReadOnlyAdmin):
    """Who may read whom, and why (INV-4). The row a privacy question is
    answered from."""

    list_display = ("candidate", "company", "basis", "granted_at", "expires_at", "revoked_at")
    list_filter = ("basis", "granted_at")
    search_fields = ("company__name", "candidate__user__email")
    readonly_fields = tuple(field.name for field in CandidateVisibilityGrant._meta.fields)


@admin.register(TalentPool)
class TalentPoolAdmin(ReadOnlyAdmin):
    list_display = ("name", "company", "created_by", "created_at")
    search_fields = ("name", "company__name")
    readonly_fields = tuple(field.name for field in TalentPool._meta.fields)


# --- records (admin inventory, docs/ADMIN.md) -------------------------------
#
# Notes, ratings and cover letters are a company's private judgement or a
# candidate's own words. The admin shows who, what and when, never the text.

from apps.common.admin_tools import RecordAdmin  # noqa: E402

from .models import (  # noqa: E402
    ApplicationNote,
    ApplicationRating,
    ApplicationTag,
    CompanyCandidateNote,
    CoverLetter,
    Tag,
    TalentPoolMember,
)


@admin.register(ApplicationNote)
class ApplicationNoteAdmin(RecordAdmin):
    private_fields = ("body", "mentions")
    safe_name = True
    list_display = ("id", "application", "author", "created_at", "deleted_at")
    date_hierarchy = "created_at"


@admin.register(ApplicationRating)
class ApplicationRatingAdmin(RecordAdmin):
    private_fields = ("comment",)
    list_display = ("application", "rater", "score", "created_at")
    list_filter = ("score",)


@admin.register(ApplicationTag)
class ApplicationTagAdmin(RecordAdmin):
    list_display = ("application", "tag", "added_by", "created_at")


@admin.register(CompanyCandidateNote)
class CompanyCandidateNoteAdmin(RecordAdmin):
    private_fields = ("body",)
    safe_name = True
    list_display = ("id", "company", "candidate", "author", "created_at", "deleted_at")
    search_fields = ("company__name",)


@admin.register(CoverLetter)
class CoverLetterAdmin(RecordAdmin):
    private_fields = ("content", "title")
    safe_name = True
    list_display = ("public_id", "profile", "job", "source", "tone", "created_at")
    list_filter = ("source", "tone")


@admin.register(Tag)
class TagAdmin(RecordAdmin):
    list_display = ("name", "company", "colour", "created_at")
    search_fields = ("name", "company__name")


@admin.register(TalentPoolMember)
class TalentPoolMemberAdmin(RecordAdmin):
    list_display = ("pool", "candidate", "added_by", "created_at", "deleted_at")
