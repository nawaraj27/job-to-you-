"""Resume ingestion reads. Scoped by owner; an unclaimed upload has none."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import QuerySet

from apps.common.ids import as_uuid

from . import content as content_rules
from .models import (
    ParseStatus,
    ProposalState,
    ResumeAnalysis,
    ResumeDocument,
    ResumeFinding,
    ResumeImportProposal,
    ResumeRender,
    ResumeTemplate,
    ResumeUpload,
    ResumeVersion,
    SuggestionState,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile


def uploads_for(profile: CandidateProfile) -> QuerySet[ResumeUpload]:
    """This candidate's uploads, with everything the history list reads.

    `documents` is prefetched because the delete confirmation tells somebody
    whether a CV was built from the file they are about to remove, and asking
    per row turns one list into one query per upload.
    """
    return (
        ResumeUpload.objects.filter(profile=profile)
        .select_related("attachment")
        .prefetch_related("parse", "documents")
    )


def uploads_in_flight(profile: CandidateProfile) -> QuerySet[ResumeUpload]:
    """Uploads still being scanned or read.

    What "come back later and see where my CV got to" needs an answer for.
    A file is in flight while the scanner has not finished with it, or while
    the reader has it — not once it has failed, because a failure is a result
    and the screens say what it was.
    """
    from apps.files.constants import ScanStatus

    return uploads_for(profile).filter(
        models.Q(attachment__scan_status=ScanStatus.PENDING)
        | models.Q(parse_status__in=(ParseStatus.PENDING, ParseStatus.RUNNING))
    )


def upload_for(profile: CandidateProfile, public_id: str) -> ResumeUpload | None:
    """One upload, scoped to its owner.

    Scoped in the query rather than fetched and then checked: the check is easy
    to forget and the queryset is not.
    """
    if as_uuid(public_id) is None:
        return None
    return (
        ResumeUpload.objects.filter(profile=profile, public_id=public_id)
        .select_related("attachment")
        .first()
    )


def open_proposals(profile: CandidateProfile) -> QuerySet[ResumeImportProposal]:
    return ResumeImportProposal.objects.filter(
        profile=profile, state=ProposalState.OFFERED
    ).select_related("parse")


def proposal_for(profile: CandidateProfile, pk: int) -> ResumeImportProposal | None:
    return ResumeImportProposal.objects.filter(profile=profile, pk=pk).first()


def latest_parse_for(profile: CandidateProfile):
    """The most recent successful parse this profile owns."""
    upload = (
        ResumeUpload.objects.filter(profile=profile, parse__isnull=False)
        .order_by("-created_at")
        .first()
    )
    return getattr(upload, "parse", None) if upload else None


# --- documents (P5) -------------------------------------------------------


def documents_for(profile: CandidateProfile) -> QuerySet[ResumeDocument]:
    """Every live CV, newest activity first."""
    return (
        ResumeDocument.objects.alive()
        .filter(profile=profile)
        .select_related("template", "current_version", "draft")
    )


def active_documents_for(profile: CandidateProfile) -> QuerySet[ResumeDocument]:
    return documents_for(profile).filter(archived_at__isnull=True)


def saved_documents_for(profile: CandidateProfile) -> QuerySet[ResumeDocument]:
    """Saved CVs only: what "Your CVs" and "Recent CVs" list (docs/CV-Lifecycle.md)."""
    return active_documents_for(profile).filter(saved_at__isnull=False)


def drafts_for(profile: CandidateProfile) -> list[ResumeDocument]:
    """Unsaved drafts, each carrying the day it expires if left alone."""
    from .services.documents import draft_expires_on

    rows = list(active_documents_for(profile).filter(saved_at__isnull=True))
    for row in rows:
        row.expires_on = draft_expires_on(row)  # type: ignore[attr-defined]
    return rows


def documents_to_apply_with(profile: CandidateProfile) -> QuerySet[ResumeDocument]:
    """Every CV that can be sent, the main one first.

    `make_primary` describes itself as "the CV applications default to", and
    the apply screen did not: it listed whatever had been edited most recently
    and pre-selected that. A candidate who had been experimenting found a list
    of sixteen documents with the newest scratch copy ticked, no indication of
    which one they had marked as their main CV, and one click between them and
    sending the wrong one.
    """
    return active_documents_for(profile).order_by("-is_primary", "-updated_at")


def document_for(profile: CandidateProfile, public_id: str) -> ResumeDocument | None:
    """One CV, scoped to its owner in the query rather than checked afterwards."""
    if as_uuid(public_id) is None:
        return None
    from django.db.models import Exists, OuterRef

    from .models import ResumeDraftSnapshot

    return (
        ResumeDocument.objects.alive()
        .filter(profile=profile, public_id=public_id)
        .select_related(
            "template", "draft", "current_version", "profile__user", "latest_improvement_job"
        )
        # Whether undo has anything to undo, read with the document rather than
        # in a query of its own: the builder screens have none to spare (NFR-2).
        .annotate(
            has_snapshots=Exists(ResumeDraftSnapshot.objects.filter(draft__document=OuterRef("pk")))
        )
        .first()
    )


def primary_document_for(profile: CandidateProfile) -> ResumeDocument | None:
    return documents_for(profile).filter(is_primary=True).first()


def versions_for(document: ResumeDocument) -> QuerySet[ResumeVersion]:
    return document.versions.select_related("created_by").all()


def versions_with_summaries(document: ResumeDocument, limit: int = 10) -> list[ResumeVersion]:
    """History rows carrying a one-line description of what is in each.

    Attached here rather than computed in the template, because it walks the
    document: a template filter that counts entries would run the walk once per
    row per render and hide the cost in a `{{ }}`.

    A redacted version keeps no content to describe, so it gets no summary and
    the row says it was erased instead.
    """
    rows = list(versions_for(document)[:limit])
    for version in rows:
        # Attached to the instance for the template to read; not a column.
        version.summary = (  # type: ignore[attr-defined]
            "" if version.is_redacted else content_rules.describe(version.content)
        )
    return rows


def version_for(document: ResumeDocument, version_no: int) -> ResumeVersion | None:
    return document.versions.filter(version_no=version_no).first()


def open_suggestions(document: ResumeDocument):
    return document.draft.suggestions.filter(state=SuggestionState.OFFERED)


def active_templates() -> QuerySet[ResumeTemplate]:
    return ResumeTemplate.objects.filter(is_active=True)


def template_by_slug(slug: str) -> ResumeTemplate | None:
    return ResumeTemplate.objects.filter(slug=slug, is_active=True).first()


# --- renders (P6) ---------------------------------------------------------


def renders_for(document: ResumeDocument) -> QuerySet[ResumeRender]:
    """Export history for one CV, newest first."""
    return (
        ResumeRender.objects.filter(version__document=document)
        .select_related("version", "template", "attachment")
        .order_by("-created_at")
    )


def renders_for_profile(profile: CandidateProfile) -> QuerySet[ResumeRender]:
    return (
        ResumeRender.objects.filter(version__document__profile=profile)
        .select_related("version", "version__document", "template", "attachment")
        .order_by("-created_at")
    )


def render_for(profile: CandidateProfile, pk: int) -> ResumeRender | None:
    """One render, scoped to its owner in the query rather than checked after."""
    return (
        ResumeRender.objects.filter(pk=pk, version__document__profile=profile)
        .select_related("attachment", "version__document")
        .first()
    )


def analyses_for(profile: CandidateProfile) -> QuerySet[ResumeAnalysis]:
    """Every report this candidate owns, newest first.

    Scoped through `version__document__profile` in the query rather than checked
    afterwards: an analysis is a reading of somebody's CV, and a selector that
    fetches first and filters second is one refactor away from not filtering.
    """
    return (
        ResumeAnalysis.objects.filter(version__document__profile=profile)
        .select_related("version", "version__document", "ai_run", "job")
        .order_by("-created_at")
    )


def analysis_for(profile: CandidateProfile, public_id: str) -> ResumeAnalysis | None:
    if as_uuid(public_id) is None:
        return None
    return (
        analyses_for(profile)
        .filter(public_id=public_id)
        .prefetch_related("findings", "checks")
        .first()
    )


def latest_analysis_for(document: ResumeDocument) -> ResumeAnalysis | None:
    return (
        ResumeAnalysis.objects.filter(version__document=document)
        .select_related("version", "ai_run", "job")
        .prefetch_related("findings", "checks")
        .first()
    )


def latest_analysis_of_profile(profile: CandidateProfile) -> ResumeAnalysis | None:
    """The newest report on any of this candidate's current CVs (REF-01).

    What "Latest analysis" in the rail means. It used to open the *main* CV's
    report, so a candidate who analysed a second CV, left, and came back was
    shown a different CV's report and could not find the one still being
    reviewed.
    """
    return (
        ResumeAnalysis.objects.filter(
            version__document__profile=profile, version__document__archived_at__isnull=True
        )
        .select_related("version__document")
        .order_by("-created_at", "-pk")
        .first()
    )


def finding_for(profile: CandidateProfile, pk: int) -> ResumeFinding | None:
    """One finding, scoped to the owner of the CV it is about."""
    return (
        ResumeFinding.objects.filter(pk=pk, analysis__version__document__profile=profile)
        .select_related("analysis", "analysis__version", "analysis__version__document")
        .first()
    )


#: What each group of import offers is called on the screen, and the order the
#: groups appear in. Contact first because it is one decision and it is the one
#: that matters most; skills last because there are always the most of them.
PROPOSAL_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("contact", "Contact details", "contact detail"),
    ("headline", "Job title", "job title"),
    ("experience", "Work experience", "role"),
    ("education", "Education", "qualification"),
    ("language", "Languages", "language"),
    ("skill", "Skills", "skill"),
)


def grouped_proposals(profile: CandidateProfile) -> list[dict]:
    """Open import offers, in groups a person can decide about.

    One flat list was what this used to be, and a CV with a long skills section
    produced twenty-four rows each labelled with its own kind — "Skill",
    "Skill", "Skill" — under a single "Add all" that accepted every one of them
    at once. Neither end of that is a decision anybody makes: the list is too
    long to read one row at a time, and "all twenty-four" is not an answer to
    any of the questions in it.

    Grouped, the same offers are four or five short lists, each with a bulk
    action that means something — "add all eight skills" is a decision, and the
    service has taken a `kind` argument since it was written.
    """
    rows = list(open_proposals(profile).select_related("parse__upload"))
    by_kind: dict[str, list] = {}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(row)

    groups = []
    for kind, title, noun in PROPOSAL_GROUPS:
        found = by_kind.pop(kind, [])
        if found:
            groups.append({"kind": kind, "title": title, "noun": noun, "proposals": found})

    # Anything the table above does not name still gets shown, under its own
    # label. A kind added to the model and forgotten here would otherwise
    # disappear from the screen entirely.
    for kind, found in by_kind.items():
        groups.append(
            {"kind": kind, "title": found[0].get_kind_display(), "noun": "item", "proposals": found}
        )
    return groups
