"""
Object-level authorization for uploads (SRS §2.5, layer 3).

The rule that carries the weight is not "the owner may read it" — it is that an
**unclaimed upload has no owner and may not be processed at all** (ADR-A32).
Every rule here has a passing negative test (DEVELOPER.md §8.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.common.errors import InvalidState, NotFound

from .models import ResumeAnalysis, ResumeDocument, ResumeRender, ResumeUpload, ResumeVersion

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile


def require_owner(upload: ResumeUpload, profile: CandidateProfile | None) -> ResumeUpload:
    """NotFound, never NotAllowed: a 403 confirms someone else's CV exists."""
    if profile is None or upload.profile_id != profile.pk:
        raise NotFound("That upload does not exist, or you cannot see it.")
    return upload


def require_processable(upload: ResumeUpload) -> ResumeUpload:
    if not upload.may_process:
        raise InvalidState(
            "That upload has not been claimed by an account, so it may not be processed."
        )
    return upload


def require_document_owner(
    document: ResumeDocument, profile: CandidateProfile | None
) -> ResumeDocument:
    """Writes and reads of a CV belong to its owner alone.

    NotFound rather than NotAllowed, for the same reason as everywhere else: a
    403 on someone else's CV confirms that it exists.
    """
    if profile is None or document.profile_id != profile.pk:
        raise NotFound("That CV does not exist, or you cannot see it.")
    return document


def require_editable(document: ResumeDocument) -> ResumeDocument:
    """A deleted CV is not editable; an archived one is read-only until restored."""
    if document.deleted_at is not None:
        raise InvalidState("That CV has been deleted.")
    if document.archived_at is not None:
        raise InvalidState("That CV is archived. Restore it before editing.")
    return document


def require_render_owner(render: ResumeRender, profile: CandidateProfile | None) -> ResumeRender:
    """Downloads belong to the CV's owner alone.

    NotFound rather than NotAllowed: a 403 on someone else's export confirms
    that the export exists, and an export is a person's CV.
    """
    if profile is None or render.version.document.profile_id != profile.pk:
        raise NotFound("That file does not exist, or you cannot open it.")
    return render


def require_analysis_owner(
    analysis: ResumeAnalysis, profile: CandidateProfile | None
) -> ResumeAnalysis:
    """A report quotes someone's CV back at them, so it is theirs alone.

    NotFound rather than NotAllowed, for the same reason as everywhere else: a
    403 confirms the report exists, and the existence of a report about a named
    person's CV is itself the thing being protected.
    """
    if profile is None or analysis.version.document.profile_id != profile.pk:
        raise NotFound("That report does not exist, or you cannot see it.")
    return analysis


def require_analysable(version: ResumeVersion) -> None:
    """A redacted version has no content left to analyse (ADR-A38)."""
    if version.redacted_at is not None:
        raise InvalidState("That version was erased and can no longer be analysed.")


def require_renderable(version: ResumeVersion) -> None:
    """A redacted version has no document left to render (ADR-A38)."""
    if version.redacted_at is not None:
        raise InvalidState("That version was erased and can no longer be exported.")
