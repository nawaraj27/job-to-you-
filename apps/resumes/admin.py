"""
Ingestion in Django admin: read-only, because everything here is a machine's
record of what happened.

The one thing staff genuinely need is diagnosis — why did this CV parse to
nothing — and the raw text is deliberately not shown in the list, because a
changelist that renders resume text puts personal data on a screen that stays
open all day.
"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    ResumeAnalysis,
    ResumeDocument,
    ResumeDraft,
    ResumeFinding,
    ResumeImportProposal,
    ResumeParse,
    ResumeRender,
    ResumeTemplate,
    ResumeUpload,
    ResumeVersion,
)


@admin.register(ResumeUpload)
class ResumeUploadAdmin(admin.ModelAdmin):
    list_display = (
        "original_filename",
        "profile",
        "mime",
        "size",
        "pages",
        "parse_status",
        "claimed_at",
        "created_at",
    )
    list_filter = ("parse_status", "mime")
    search_fields = ("original_filename", "sha256", "public_id")
    readonly_fields = tuple(field.name for field in ResumeUpload._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ResumeParse)
class ResumeParseAdmin(admin.ModelAdmin):
    list_display = ("upload", "engine", "engine_version", "duration_ms", "created_at")
    list_filter = ("engine", "engine_version")
    readonly_fields = tuple(field.name for field in ResumeParse._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ResumeImportProposal)
class ResumeImportProposalAdmin(admin.ModelAdmin):
    list_display = ("label", "kind", "profile", "state", "confidence", "decided_at")
    list_filter = ("kind", "state")
    readonly_fields = tuple(field.name for field in ResumeImportProposal._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        # Deciding a proposal is the candidate's, through their own screen.
        return False


@admin.register(ResumeTemplate)
class ResumeTemplateAdmin(admin.ModelAdmin):
    """The one editable table in this app — templates are content (ADR-A5)."""

    list_display = ("name", "slug", "family", "layout", "tier", "is_active", "order")
    list_filter = ("layout", "tier", "is_active")
    search_fields = ("name", "slug", "family")
    prepopulated_fields = {"slug": ("name",)}

    def has_delete_permission(self, request, obj=None) -> bool:
        # A document in use points at its template with PROTECT; retiring is
        # `is_active=False`, which keeps existing CVs rendering.
        return False


@admin.register(ResumeDocument)
class ResumeDocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "profile", "kind", "template", "is_primary", "updated_at")
    list_filter = ("kind", "builder_mode", "is_primary")
    search_fields = ("name", "public_id")
    readonly_fields = tuple(field.name for field in ResumeDocument._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ResumeVersion)
class ResumeVersionAdmin(admin.ModelAdmin):
    """Read-only by construction: the rows are immutable (ADR-A8).

    Content is deliberately absent from the changelist. A staff screen that
    renders CV text is a screen left open on a desk all day.
    """

    list_display = (
        "document",
        "version_no",
        "created_reason",
        "finalized_at",
        "redacted_at",
        "sha256",
    )
    list_filter = ("created_reason",)
    search_fields = ("sha256",)
    readonly_fields = tuple(field.name for field in ResumeVersion._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # Deletion would break the evidentiary chain an application depends on.
        # Erasure is redaction, through the service (ADR-A38).
        return False


@admin.register(ResumeDraft)
class ResumeDraftAdmin(admin.ModelAdmin):
    list_display = ("document", "row_version", "dirty", "saved_at", "updated_at")
    readonly_fields = ("document", "content", "content_schema_version", "row_version", "saved_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ResumeRender)
class ResumeRenderAdmin(admin.ModelAdmin):
    """Export history. Read-only: every row is a file that already exists.

    The candidate's name is not in the changelist and neither is the content —
    a staff screen open all day should not be a CV reader.
    """

    list_display = (
        "version",
        "format",
        "template",
        "engine",
        "engine_version",
        "pages",
        "duration_ms",
        "created_at",
    )
    list_filter = ("format", "engine_version", "template")
    search_fields = ("sha256",)
    readonly_fields = tuple(field.name for field in ResumeRender._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # Deleting the row without the file leaves an orphan in object storage;
        # invalidation goes through resumes.services.renders.invalidate().
        return False


@admin.register(ResumeAnalysis)
class ResumeAnalysisAdmin(admin.ModelAdmin):
    """Report history. Read-only, and it shows the score rather than the text.

    Staff need to answer "why did this CV score 41" and "did the model run".
    Neither question needs the findings on a changelist that stays open all day,
    and the findings quote the CV.
    """

    list_display = (
        "public_id",
        "version",
        "score",
        "algorithm_version",
        "ai_status",
        "target_role",
        "created_at",
    )
    list_filter = ("algorithm_version", "score")
    search_fields = ("public_id", "target_role")
    readonly_fields = tuple(field.name for field in ResumeAnalysis._meta.fields)
    raw_id_fields = ("version", "job", "ai_run")

    @admin.display(description="AI", boolean=True)
    def ai_status(self, obj: ResumeAnalysis) -> bool:
        return obj.has_ai

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        # A report is a record of what a scorer said at a point in time. A
        # record staff can edit is not a record.
        return False


@admin.register(ResumeFinding)
class ResumeFindingAdmin(admin.ModelAdmin):
    """Findings, for tracing a prompt change or a scorer change across reports.

    `code` and `severity` are the columns that matter: they are what a staff
    member counts when asking whether a new prompt version started producing
    something new.
    """

    list_display = ("code", "severity", "from_ai", "analysis", "order")
    list_filter = ("severity", "from_ai", "code")
    search_fields = ("code", "title")
    readonly_fields = tuple(field.name for field in ResumeFinding._meta.fields)
    raw_id_fields = ("analysis",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


# --- records (admin inventory, docs/ADMIN.md) -------------------------------
#
# The rest of the CV machinery: state and timing only. Draft snapshots, builder
# transcripts and suggestions are a candidate's CV text and stay out.

from apps.common.admin_tools import RecordAdmin  # noqa: E402

from .models import (  # noqa: E402
    BuilderSession,
    ImprovementJob,
    ResumeAtsCheck,
    ResumeDraftSnapshot,
    ResumeSuggestion,
)


@admin.register(ImprovementJob)
class ImprovementJobAdmin(RecordAdmin):
    private_fields = ("payload", "result", "request_token", "stage_detail")
    list_display = (
        "public_id",
        "document",
        "kind",
        "state",
        "stage",
        "attempts",
        "started_at",
        "finished_at",
    )
    list_filter = ("kind", "state", "stage")
    date_hierarchy = "created_at"


@admin.register(BuilderSession)
class BuilderSessionAdmin(RecordAdmin):
    private_fields = ("transcript", "step_state")
    list_display = ("document", "mode", "created_at", "updated_at")
    list_filter = ("mode",)


@admin.register(ResumeDraftSnapshot)
class ResumeDraftSnapshotAdmin(RecordAdmin):
    private_fields = ("content",)
    safe_name = True
    list_display = ("id", "draft", "seq", "label", "created_at")


@admin.register(ResumeSuggestion)
class ResumeSuggestionAdmin(RecordAdmin):
    private_fields = ("original", "proposed", "rationale")
    safe_name = True
    list_display = ("id", "draft", "target_path", "source", "state", "decided_at")
    list_filter = ("source", "state")


@admin.register(ResumeAtsCheck)
class ResumeAtsCheckAdmin(RecordAdmin):
    private_fields = ("note",)
    list_display = ("analysis", "key", "label", "status", "order")
    list_filter = ("status", "key")
