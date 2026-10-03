"""
Documents: creating one, naming it, choosing its template, retiring it.

A document is created three ways — blank, from a parsed upload, or as a copy —
and all three go through here so that every one of them ends up with a draft, a
template and a coherent primary flag. A `ResumeDocument` without a
`ResumeDraft` is a document the builder cannot open, so the two are created in
one transaction or not at all.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.notifications import events

from .. import content as content_rules
from ..models import (
    BuilderMode,
    DocumentKind,
    ParseStatus,
    ResumeDocument,
    ResumeDraft,
    ResumeParse,
    ResumeTemplate,
    ResumeUpload,
    TemplateTier,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile

#: A candidate with forty CVs has none. High enough never to be hit by real
#: use, low enough that an automated loop cannot fill the table.
MAX_DOCUMENTS = 20

#: Unsaved drafts at once (docs/CV-Lifecycle.md). Opening the builder five
#: times used to leave five "CVs"; now each is a draft, and a sixth asks the
#: candidate to save or delete one rather than piling up.
MAX_DRAFTS = 5


@transaction.atomic
def create_document(
    *,
    profile: CandidateProfile,
    actor: User,
    name: str = "",
    template: ResumeTemplate | None = None,
    mode: str = BuilderMode.EDITOR,
    content: dict | None = None,
    kind: str = DocumentKind.BUILT,
    upload: ResumeUpload | None = None,
) -> ResumeDocument:
    """Create a document and its draft together.

    It starts as an unsaved draft (`saved_at` is null) and becomes a saved CV
    the first time a version is frozen (`versions.finalize`).
    """
    _guard_count(profile)
    _guard_drafts(profile)

    if mode not in BuilderMode.values:
        raise InvalidState("That is not a builder mode.")

    chosen = template or default_template()
    require_template_allowed(profile=profile, template=chosen)
    document = ResumeDocument.objects.create(
        profile=profile,
        name=(name or "My CV").strip()[:120],
        kind=kind,
        upload=upload,
        template=chosen,
        builder_mode=mode,
        # The first CV is the primary one. Nobody wants to be asked.
        is_primary=not ResumeDocument.objects.alive().filter(profile=profile).exists(),
    )
    ResumeDraft.objects.create(
        document=document,
        content=content or content_rules.blank_document(name=profile.user.full_name),
        content_schema_version=content_rules.CONTENT_SCHEMA_VERSION,
    )

    events.emit(
        "resume.document_created",
        document,
        payload={
            "document_public_id": str(document.public_id),
            "profile_public_id": str(profile.public_id),
            "kind": kind,
        },
    )
    audit.record(
        verb="resume.document_created", actor=actor, target=document, metadata={"kind": kind}
    )
    return document


@transaction.atomic
def create_from_upload(
    *, upload: ResumeUpload, actor: User, template: ResumeTemplate | None = None
) -> ResumeDocument:
    """Turn a parsed CV into a document the candidate can edit.

    The parse is read here rather than re-run: P4 already produced it, and
    reading it again would be a second answer to a question already asked.
    """
    if upload.profile_id is None:
        raise InvalidState("That upload has not been claimed.")
    if upload.parse_status != ParseStatus.PARSED:
        raise InvalidState("That CV has not been read yet. Try again in a moment.")

    parse = getattr(upload, "parse", None)
    profile = upload.profile
    if parse is None or profile is None:
        raise InvalidState("That CV has not been read yet.")

    existing = ResumeDocument.objects.alive().filter(upload=upload).first()
    if existing is not None:
        # Idempotent: clicking "edit this CV" twice opens the same document
        # rather than forking the candidate's work in half.
        return existing

    return create_document(
        profile=profile,
        actor=actor,
        name=upload.original_filename.rsplit(".", 1)[0][:120] or "My CV",
        template=template,
        kind=DocumentKind.UPLOADED,
        upload=upload,
        content=_content_from_parse(parse, profile=profile),
    )


@transaction.atomic
def rename(*, document: ResumeDocument, actor: User, name: str) -> ResumeDocument:
    cleaned = (name or "").strip()
    if not cleaned:
        raise InvalidState("Give the CV a name.")

    document.name = cleaned[:120]
    document.save(update_fields=["name", "updated_at"])
    audit.record(verb="resume.document_renamed", actor=actor, target=document)
    return document


@transaction.atomic
def set_template(
    *, document: ResumeDocument, actor: User, template: ResumeTemplate
) -> ResumeDocument:
    """Change how the document looks. Never touches what it says."""
    if not template.is_active:
        raise InvalidState("That template is no longer available.")
    require_template_allowed(profile=document.profile, template=template)

    document.template = template
    document.save(update_fields=["template", "updated_at"])
    audit.record(
        verb="resume.template_changed",
        actor=actor,
        target=document,
        metadata={"template": template.slug},
    )
    return document


def set_mode(*, document: ResumeDocument, actor: User, mode: str) -> ResumeDocument:
    """Switch builder. The document and its draft are shared across all three.

    A switch to the mode the document is already in is not a switch. Opening
    the editor used to write the row and bump `updated_at` on every page load,
    which cost the screen an UPDATE inside a transaction it did not need — and
    made "when did I last change this CV" mean "when did I last look at it".

    The guard sits outside the transaction deliberately: the common case is a
    reload, and opening one to decide not to write it is the cost this is
    removing.
    """
    if mode not in BuilderMode.values:
        raise InvalidState("That is not a builder mode.")

    if document.builder_mode == mode:
        return document

    return _write_mode(document=document, mode=mode)


@transaction.atomic
def _write_mode(*, document: ResumeDocument, mode: str) -> ResumeDocument:
    document.builder_mode = mode
    document.save(update_fields=["builder_mode", "updated_at"])
    return document


@transaction.atomic
def make_primary(*, document: ResumeDocument, actor: User) -> ResumeDocument:
    """The CV applications default to.

    The other one is cleared first: the partial unique index would otherwise
    reject the write, and "one primary" is a database guarantee rather than a
    hope.
    """
    if document.deleted_at is not None:
        raise InvalidState("That CV has been deleted.")

    ResumeDocument.objects.alive().filter(profile=document.profile, is_primary=True).exclude(
        pk=document.pk
    ).update(is_primary=False)

    document.is_primary = True
    document.save(update_fields=["is_primary", "updated_at"])
    audit.record(verb="resume.primary_set", actor=actor, target=document)
    return document


@transaction.atomic
def archive(*, document: ResumeDocument, actor: User) -> ResumeDocument:
    """Out of the way, still readable.

    A primary CV cannot be archived: applications default to it, and archiving
    it would leave the candidate with a default they cannot see.
    """
    if document.is_primary:
        raise InvalidState("Make another CV primary before archiving this one.")

    document.archived_at = timezone.now()
    document.save(update_fields=["archived_at", "updated_at"])
    audit.record(verb="resume.document_archived", actor=actor, target=document)
    return document


@transaction.atomic
def restore(*, document: ResumeDocument, actor: User) -> ResumeDocument:
    document.archived_at = None
    document.save(update_fields=["archived_at", "updated_at"])
    return document


@transaction.atomic
def delete(*, document: ResumeDocument, actor: User) -> ResumeDocument:
    """Soft delete a saved CV (ADR-A12); remove a never-saved one outright.

    Soft because a finalized version may be attached to an application, and an
    employer's copy of a CV must not vanish because the candidate tidied up.
    A document with no version has no such copy, so it goes completely and an
    old link to it stops working (docs/CV-Lifecycle.md).
    """
    if (
        document.is_primary
        and ResumeDocument.objects.alive()
        .filter(profile=document.profile)
        .exclude(pk=document.pk)
        .exists()
    ):
        raise InvalidState("Make another CV primary before deleting this one.")

    if not document.versions.exists():
        # Never saved: no version, so no application or employer copy can
        # point at it. Removed outright, with its draft, snapshots and
        # conversation, so nothing of it is reachable by an old link.
        audit.record(
            verb="resume.document_deleted",
            actor=actor,
            target=document,
            metadata={"hard": True, "name": document.name},
        )
        document.delete()
        return document

    document.deleted_at = timezone.now()
    document.is_primary = False
    document.save(update_fields=["deleted_at", "is_primary", "updated_at"])
    audit.record(verb="resume.document_deleted", actor=actor, target=document)
    return document


@transaction.atomic
def duplicate(*, document: ResumeDocument, actor: User) -> ResumeDocument:
    """Copy a CV so a tailored variant does not overwrite the original."""
    draft = document.draft
    return create_document(
        profile=document.profile,
        actor=actor,
        name=f"{document.name} (copy)"[:120],
        template=document.template,
        mode=document.builder_mode,
        kind=document.kind,
        content=draft.content,
    )


def require_template_allowed(*, profile: CandidateProfile, template: ResumeTemplate) -> None:
    """Refuse a Pro template to somebody who is not on Pro (P18).

    Here rather than in the gallery view, because the gallery is not the only
    way to reach a template: the builder posts a slug, and a limit enforced by
    hiding a card is a limit that stops applying the moment somebody posts the
    form directly.
    """
    if template.tier != TemplateTier.PRO:
        return

    from apps.billing.services import entitlements

    entitlements.require(profile, "pro_templates")


def default_template() -> ResumeTemplate:
    """The template a new document starts in.

    A free one by preference, so the platform's own default is never something
    the candidate is then refused. Raises rather than creating one: an empty
    template table means the seed did not run, and inventing a row here would
    hide that until somebody exported a CV with no layout.
    """
    active = ResumeTemplate.objects.filter(is_active=True).order_by("order", "name")
    template = active.filter(tier=TemplateTier.FREE).first() or active.first()
    if template is None:
        raise NotFound("No resume templates are configured. Run `manage.py seed_resume_templates`.")
    return template


def _guard_drafts(profile: CandidateProfile) -> None:
    unsaved = ResumeDocument.objects.alive().filter(
        profile=profile, saved_at__isnull=True, archived_at__isnull=True
    )
    if unsaved.count() >= MAX_DRAFTS:
        raise Conflict(
            f"You have {MAX_DRAFTS} unsaved drafts. Save one or delete one before starting another."
        )


def draft_expires_on(document: ResumeDocument):  # type: ignore[no-untyped-def]
    """The day an unsaved draft is removed if nobody touches it again."""
    from django.conf import settings

    if document.saved_at is not None or document.archived_at is not None:
        return None
    # A document whose draft row is missing (an older or imported record) is
    # dated from the document itself rather than failing the whole list.
    try:
        touched = max(document.updated_at, document.draft.updated_at)
    except ResumeDraft.DoesNotExist:
        touched = document.updated_at
    days = int(getattr(settings, "RESUME_DRAFT_RETENTION_DAYS", 14))
    return (touched + dt.timedelta(days=days)).date()


def purge_abandoned_drafts(*, now=None) -> int:  # type: ignore[no-untyped-def]
    """Remove unsaved drafts nobody has edited for RESUME_DRAFT_RETENTION_DAYS.

    Only drafts that were never saved, are not archived and have no version
    (so nothing else can refer to them). Saved and archived CVs are never
    touched. Idempotent: a second run finds nothing left to remove.
    """
    from django.conf import settings

    now = now or timezone.now()
    days = int(getattr(settings, "RESUME_DRAFT_RETENTION_DAYS", 14))
    cutoff = now - dt.timedelta(days=days)
    stale = ResumeDocument.objects.filter(
        saved_at__isnull=True,
        archived_at__isnull=True,
        updated_at__lt=cutoff,
        draft__updated_at__lt=cutoff,
        versions__isnull=True,
    ).distinct()
    removed = 0
    for document in stale:
        with transaction.atomic():
            locked = ResumeDocument.objects.select_for_update().filter(pk=document.pk).first()
            if locked is None or locked.saved_at is not None or locked.versions.exists():
                continue  # saved, versioned or already removed since the query
            audit.record(
                verb="resume.draft_expired",
                actor=None,
                target=locked,
                metadata={"name": locked.name, "days": days},
            )
            was_primary, profile = locked.is_primary, locked.profile
            locked.delete()
            if was_primary:
                _promote_a_saved_cv(profile)
            removed += 1
    return removed


def _promote_a_saved_cv(profile: CandidateProfile) -> None:
    """Keep a main CV when the expired draft was it.

    The first document a candidate creates is their main CV, saved or not. If
    it expires unsaved, the newest saved CV takes its place, so applications
    still have a default; with none saved there is simply no main CV yet.
    """
    if ResumeDocument.objects.alive().filter(profile=profile, is_primary=True).exists():
        return
    successor = (
        ResumeDocument.objects.alive()
        .filter(profile=profile, saved_at__isnull=False, archived_at__isnull=True)
        .order_by("-updated_at")
        .first()
    )
    if successor is not None:
        successor.is_primary = True
        successor.save(update_fields=["is_primary", "updated_at"])


def _guard_count(profile: CandidateProfile) -> None:
    if ResumeDocument.objects.alive().filter(profile=profile).count() >= MAX_DOCUMENTS:
        raise Conflict(
            f"You can keep up to {MAX_DOCUMENTS} CVs. Delete one before creating another."
        )


def _content_from_parse(parse: ResumeParse, *, profile: CandidateProfile) -> dict:
    """Build a document from what P4 read, marking every entry's origin.

    Everything imported starts `detached`: it came from the candidate's own
    file, not from their profile, so the reconcile protocol must not treat it
    as profile-owned and quietly overwrite it (§5.5.3).
    """
    structured = parse.structured or {}
    contact = structured.get("contact", {})
    content = content_rules.blank_document(
        # The name on the CV wins over the name on the account. Both are the
        # candidate's, but the document is the one they are about to send, and
        # taking its email from the file while taking its name from the account
        # produced a CV with one person's name over another's history.
        name=structured.get("name") or profile.user.full_name,
        headline=structured.get("headline", ""),
        email=next(iter(contact.get("emails", [])), profile.user.email),
    )
    content["contact"]["phone"] = next(iter(contact.get("phones", [])), "")
    # Read from the CV's own header, so it is the candidate's, not a guess.
    content["contact"]["location"] = contact.get("location", "")
    content["summary"] = structured.get("summary", "")

    summary_section = content_rules.section_by_key(content, "summary")
    if summary_section is not None:
        summary_section["text"] = structured.get("summary", "")

    experience = content_rules.section_by_key(content, "experience")
    if experience is not None:
        experience["entries"] = [
            content_rules.new_entry(
                title=row.get("title", ""),
                org=row.get("org", ""),
                dates=_dates(row),
                bullets=list(row.get("bullets", []) or []),
            )
            for row in structured.get("experience", [])
        ]

    education = content_rules.section_by_key(content, "education")
    if education is not None:
        education["entries"] = [
            content_rules.new_entry(
                title=row.get("qualification", ""),
                org=row.get("institution", ""),
                dates=_dates(row),
            )
            for row in structured.get("education", [])
        ]

    skills = content_rules.section_by_key(content, "skills")
    if skills is not None:
        skills["items"] = list(structured.get("skills", []))

    languages = content_rules.section_by_key(content, "languages")
    if languages is not None and structured.get("languages"):
        languages["items"] = list(structured["languages"])
        languages["visible"] = True

    _carry_the_rest(content, structured)
    return content


#: The sections a CV built out of anything other than employment depends on,
#: and how each one is shaped in a document.
CARRIED_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("projects", "Projects", "entries"),
    ("certifications", "Certifications", "list"),
    ("awards", "Awards", "list"),
    ("publications", "Publications", "list"),
)


def _carry_the_rest(content: dict, structured: dict) -> None:
    """Projects, certifications, awards and publications.

    These were read by the parser and carried nowhere. A student uploaded a CV
    whose projects were the best thing on it, pressed "Edit as a CV", and
    opened a document with no projects in it; an academic lost their
    publications the same way, and a nurse lost their registration. Silently
    dropping the part of somebody's CV they are proudest of is the worst thing
    an importer can do after inventing something.

    Only sections the file actually has are added, so a CV with no projects
    still opens without an empty Projects heading on it.
    """
    for key, title, kind in CARRIED_SECTIONS:
        values = structured.get(key) or []
        if not values:
            continue

        section = content_rules.ensure_section(content, key=key, title=title, kind=kind)
        if kind == "entries":
            section["entries"] = [
                content_rules.new_entry(
                    title=str(row.get("name", ""))[:160],
                    bullets=[str(line) for line in row.get("bullets", []) or []],
                )
                for row in values
                if str(row.get("name", "")).strip()
            ]
        else:
            section["items"] = [str(value) for value in values]


def _dates(row: dict) -> str:
    start, end = row.get("start", ""), row.get("end", "")
    if row.get("is_current"):
        return f"{start} — present" if start else "present"
    if start and end:
        return f"{start} — {end}"
    return start or end or ""
