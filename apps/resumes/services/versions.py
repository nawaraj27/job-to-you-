"""
Versions: the four acts that create one, and the one change permitted after.

A version is created by exactly four acts — the candidate saves, requests an
analysis, submits an application, or exports (SRS §5.5.1). Autosave is not one
of them, and this module is where that stays true.

**Analysis reuses a version whose content has not changed.** Without that
check, ten clicks on Analyse produce ten identical immutable rows — version
spam arriving by a different route than the autosave the draft/version split
was built to stop.

After finalization the row is immutable, enforced three ways: `finalized_at` is
NOT NULL, `save()` refuses, and a Postgres trigger rejects the write whatever
issued it. Redaction is the single permitted transition (ADR-A38), and it is
single-use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState
from apps.notifications import events

from .. import content as content_rules
from ..models import ResumeDocument, ResumeDraft, ResumeVersion, VersionReason

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User


@transaction.atomic
def finalize(
    *,
    document: ResumeDocument,
    actor: User | None,
    reason: str,
    change_note: str = "",
) -> ResumeVersion:
    """Freeze the draft into an immutable version, or reuse the current one.

    Reuse is not an optimisation. Two versions with identical content are two
    answers to "what did they submit", and the history stops being readable
    long before it stops being correct.
    """
    if reason not in VersionReason.values:
        raise InvalidState(f"{reason!r} is not a reason to create a version.")

    draft = document.draft
    if content_rules.is_empty(draft.content):
        raise InvalidState(
            "There is nothing in this CV yet. Add some detail before saving a version."
        )

    digest = content_rules.content_hash(draft.content)
    current = document.current_version
    # Each of the four acts that freeze a version is the candidate keeping
    # this CV: from here on it is a saved CV, not an expiring draft.
    _mark_saved(document)

    if current is not None and current.sha256 == digest and not current.is_redacted:
        # Nothing has changed since the last version. The draft is marked clean
        # so the interface stops offering a save that would do nothing.
        _mark_clean(draft)
        return current

    next_number = (document.versions.aggregate(top=Max("version_no"))["top"] or 0) + 1

    version = ResumeVersion.objects.create(
        document=document,
        version_no=next_number,
        content=draft.content,
        content_schema_version=draft.content_schema_version,
        template_snapshot=document.template.snapshot(),
        sha256=digest,
        created_by=actor,
        created_reason=reason,
        change_note=(change_note or "").strip()[:200],
        finalized_at=timezone.now(),
    )

    document.current_version = version
    document.save(update_fields=["current_version", "updated_at"])
    _mark_clean(draft)

    events.emit(
        "resume.version_created",
        version,
        payload={
            "document_public_id": str(document.public_id),
            "version_no": version.version_no,
            "reason": reason,
            # The hash, never the content: the outbox has a different retention
            # from the version and must not accumulate CV text.
            "sha256": digest,
        },
    )
    audit.record(
        verb="resume.version_created",
        actor=actor,
        target=version,
        metadata={"document": str(document.public_id), "version_no": next_number, "reason": reason},
    )
    return version


@transaction.atomic
def restore(*, document: ResumeDocument, actor: User, version: ResumeVersion) -> ResumeDocument:
    """Bring an old version back into the draft.

    Copies into the draft rather than rewinding the version list: the history
    is a record of what was submitted, and deleting part of it to "go back"
    would rewrite that record.
    """
    if version.document_id != document.pk:
        raise InvalidState("That version belongs to a different CV.")
    if version.is_redacted:
        raise InvalidState("That version was erased and cannot be restored.")

    from . import drafts as draft_service

    draft = document.draft
    # Labelled, because the label is what the Undo button is offering to take
    # back. Restoring replaces the working draft, and a candidate who has been
    # editing needs a way out that is not "restore the other one and hope".
    draft_service.replace(
        draft=draft,
        content=version.content,
        label=f"restore:v{version.version_no}",
    )

    audit.record(
        verb="resume.version_restored",
        actor=actor,
        target=document,
        metadata={"version_no": version.version_no},
    )
    return document


@transaction.atomic
def redact(
    *, version: ResumeVersion, actor: User | None, reason: str = "Erasure request"
) -> ResumeVersion:
    """Replace a version's content with a tombstone (ADR-A38).

    The one permitted change to a finalized row, and the reason privacy erasure
    is possible at all: the version cannot be deleted while an application
    references it, so its contents are replaced and everything evidentiary —
    including `sha256` — is left byte-identical.

    A hash of destroyed content is not personal data, and it is what still
    proves *that* a document was submitted and when.
    """
    if version.is_redacted:
        raise InvalidState("That version has already been erased.")

    version.content = content_rules.TOMBSTONE
    version.redacted_at = timezone.now()
    version.redaction_reason = (reason or "")[:120]
    version.redacted_by = actor
    version.save(update_fields=["content", "redacted_at", "redaction_reason", "redacted_by"])

    events.emit(
        "resume.version_redacted",
        version,
        payload={
            "document_public_id": str(version.document.public_id),
            "version_no": version.version_no,
        },
    )
    # The tombstone is worthless if the original PDF is still downloadable.
    from . import renders as render_service

    render_service.invalidate(version=version, actor=actor)

    audit.record(
        verb="resume.version_redacted",
        actor=actor,
        target=version,
        metadata={"document": str(version.document.public_id), "version_no": version.version_no},
    )
    return version


def _mark_clean(draft: ResumeDraft) -> None:
    draft.dirty = False
    draft.saved_at = timezone.now()
    draft.save(update_fields=["dirty", "saved_at", "updated_at"])


def _mark_saved(document: ResumeDocument) -> None:
    """Stamp the first save (docs/CV-Lifecycle.md). Idempotent."""
    if document.saved_at is not None:
        return
    from django.utils import timezone

    now = timezone.now()
    ResumeDocument.objects.filter(pk=document.pk, saved_at__isnull=True).update(saved_at=now)
    document.saved_at = now
