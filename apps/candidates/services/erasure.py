"""
"Delete everything" — the button on the data settings screen.

What it deletes is the *profile*, not the account: the sections, the skills, the
contact details and the salary expectation. The `User` row survives because the
person may still be signed in, may still hold applications an employer is
legally obliged to keep, and may want to start again without registering twice.
Account closure and legal erasure are a separate flow (SRS §19.4) and are not
this button.

The screen says exactly that before the confirmation, because a delete button
that means something narrower than it says is worse than no delete button.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from django.db import transaction

from apps.audit import services as audit
from apps.common.errors import InvalidState
from apps.notifications import events

from ..models import CandidateProfile

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.files.models import Attachment

CONFIRMATION = "DELETE"

#: Cleared back to the empty profile. Listed rather than looped over the model's
#: fields, so adding a field is a decision about erasure rather than an
#: accident either way.
_RESET_FIELDS = {
    "headline": "",
    "summary": "",
    "phone": "",
    "location": None,
    "notice_period_days": None,
    "seeking": "",
    "open_to": list,
    "salary_min": None,
    "salary_max": None,
    "currency": "",
    "period": "",
    "salary_min_base": None,
    "salary_max_base": None,
    "fx_rate_id": None,
    "fx_rate_value": None,
    "fx_as_of": None,
    "fx_source": "",
}


@dataclass(frozen=True)
class ErasureResult:
    sections_cleared: int
    rows_deleted: int


@transaction.atomic
def erase_profile_data(
    *, profile: CandidateProfile, actor: User, confirmation: str
) -> ErasureResult:
    """Empty a profile. Typing the confirmation word is the only guard."""
    if (confirmation or "").strip().upper() != CONFIRMATION:
        raise InvalidState(f"Type {CONFIRMATION} to confirm. Nothing has been removed.")

    related = (
        profile.experiences,
        profile.education,
        profile.projects,
        profile.certifications,
        profile.languages,
        profile.links,
        profile.awards,
        profile.skills,
        profile.blocked_companies,
    )

    rows = 0
    cleared = 0
    for manager in related:
        deleted, _per_model = manager.all().delete()
        rows += deleted
        cleared += int(deleted > 0)

    for field, value in _RESET_FIELDS.items():
        setattr(profile, field, value() if callable(value) else value)

    profile.completeness = 0
    profile.is_searchable = False
    profile.row_version += 1
    profile.save()

    events.emit(
        "candidate.data_erased",
        profile,
        payload={"user_id": actor.pk, "sections_cleared": cleared},
    )
    audit.record(
        verb="candidate.data_erased",
        actor=actor,
        target=profile,
        metadata={"rows_deleted": rows},
    )
    return ErasureResult(sections_cleared=cleared, rows_deleted=rows)


def export_profile(profile: CandidateProfile) -> dict:
    """Everything this profile holds, as plain data (SRS §19.4, portability).

    Built by hand rather than by serialising the model, so adding a field is a
    deliberate decision about what leaves the system — and so internal columns
    (`row_version`, derived FX provenance, primary keys) stay out of a file the
    candidate is told is "their data".
    """
    data = {
        "profile": {
            "headline": profile.headline,
            "summary": profile.summary,
            "location": str(profile.location) if profile.location_id else None,
            "phone": profile.phone,
            "seeking": profile.seeking,
            "open_to": profile.open_to,
            "notice_period_days": profile.notice_period_days,
            "salary": {
                "min": _number(profile.salary_min),
                "max": _number(profile.salary_max),
                "currency": profile.currency,
                "period": profile.period,
            },
            "visibility": profile.visibility,
            "completeness": profile.completeness,
        },
        "skills": [
            {
                "skill": record.skill.name,
                "years": _number(record.years),
                "level": record.get_level_display() if record.level else None,
                "source": record.source,
            }
            for record in profile.skills.select_related("skill")
        ],
        "experience": [
            {
                "title": row.title,
                "organisation": row.org_name,
                "start_date": _date(row.start_date),
                "end_date": _date(row.end_date),
                "is_current": row.is_current,
                "summary": row.summary,
                "bullets": row.bullets,
            }
            for row in profile.experiences.all()
        ],
        "education": [
            {
                "institution": row.institution,
                "qualification": row.qualification,
                "field_of_study": row.field_of_study,
                "start_date": _date(row.start_date),
                "end_date": _date(row.end_date),
            }
            for row in profile.education.all()
        ],
        "projects": [
            {"name": row.name, "url": row.url, "summary": row.summary}
            for row in profile.projects.all()
        ],
        "certifications": [
            {
                "name": row.name,
                "issuer": row.issuer,
                "issued_on": _date(row.issued_on),
                "expires_on": _date(row.expires_on),
                "credential_id": row.credential_id,
            }
            for row in profile.certifications.all()
        ],
        "languages": [
            {"language": row.language.name, "proficiency": row.get_proficiency_display()}
            for row in profile.languages.select_related("language")
        ],
        "links": [
            {"kind": row.kind, "label": row.label, "url": row.url} for row in profile.links.all()
        ],
        "awards": [
            {"name": row.name, "issuer": row.issuer, "awarded_on": _date(row.awarded_on)}
            for row in profile.awards.all()
        ],
    }
    # FR-7.6 puts messages in the export and SRS §20 calls it the access right.
    # It carried only the profile sections: nothing the candidate applied to,
    # wrote to an employer, or had read about them.
    data.update(_activity(profile))
    return data


#: Upper bound on original files packed into one archive. Past it, files are
#: listed in the manifest as left out rather than silently dropped, so the
#: candidate knows what to download separately.
ARCHIVE_FILE_BYTES_LIMIT = 100 * 1024 * 1024


def original_files(profile: CandidateProfile) -> list[tuple[str, Attachment]]:
    """The candidate's own original uploads, as (folder, attachment) pairs.

    FR-1.9 is "JSON plus original files". Only files this person supplied:
    uploaded CVs, certificate files on their profile, and attachments they sent
    in messages. Never employer files, renders the platform generated, or
    anything not scanned clean.
    """
    from apps.messaging.models import Message, SenderKind
    from apps.resumes.models import ResumeUpload

    rows: list[tuple[str, Attachment]] = []
    seen: set[int] = set()

    def add(folder: str, attachment: Attachment | None) -> None:
        if attachment is None or attachment.pk in seen or not attachment.is_servable:
            return
        seen.add(attachment.pk)
        rows.append((folder, attachment))

    for upload in ResumeUpload.objects.filter(profile=profile).select_related("attachment"):
        add("cvs", upload.attachment)
    for certification in profile.certifications.select_related("file"):
        add("certificates", certification.file)
    sent = Message.objects.filter(
        conversation__candidate=profile, sender_kind=SenderKind.CANDIDATE
    ).prefetch_related("attachments")
    for message in sent:
        for attachment in message.attachments.all():
            if attachment.uploaded_by_id == profile.user_id:
                add("message-attachments", attachment)
    return rows


def build_archive(*, profile: CandidateProfile, actor: User, request_id: str = "") -> IO[bytes]:
    """A zip of `careerai-export.json` plus the original files (FR-1.9).

    Returns an open, rewound temporary file. Every packed file is logged as an
    access (`data_export`), because a private file leaving the system is a read
    like any other.
    """
    import json
    import tempfile
    import zipfile

    from django.core.files.storage import storages

    from apps.files import services as file_services

    data = export_profile(profile)
    included: list[dict[str, object]] = []
    left_out: list[dict[str, object]] = []
    used_names: set[str] = set()
    total = 0

    spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)  # noqa: SIM115 - returned open
    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for folder, attachment in original_files(profile):
            if total + attachment.size > ARCHIVE_FILE_BYTES_LIMIT:
                left_out.append(
                    {"folder": folder, "filename": attachment.filename, "reason": "size limit"}
                )
                continue
            name = f"files/{folder}/{attachment.filename}"
            if name in used_names:
                name = f"files/{folder}/{attachment.public_id}-{attachment.filename}"
            used_names.add(name)

            with storages[attachment.storage_alias].open(attachment.storage_key, "rb") as source:
                archive.writestr(name, source.read())
            file_services.record_access(
                attachment=attachment, actor=actor, purpose="data_export", request_id=request_id
            )
            total += attachment.size
            included.append({"path": name, "size": attachment.size, "type": attachment.mime})

        data["original_files"] = {"included": included, "left_out": left_out}
        archive.writestr(
            "careerai-export.json", json.dumps(data, indent=2, ensure_ascii=False, default=str)
        )

    audit.record(
        verb="candidate.data_exported",
        actor=actor,
        target=profile,
        metadata={"files": len(included), "left_out": len(left_out)},
    )
    spool.seek(0)
    return spool


#: The export keys that are sections of the profile itself, for the data screen.
PROFILE_SECTIONS = (
    "skills",
    "experience",
    "education",
    "projects",
    "certifications",
    "languages",
    "links",
    "awards",
)


def _activity(profile: CandidateProfile) -> dict[str, list[dict[str, object]]]:
    """What the candidate did and what was done with their profile.

    Stage names are the candidate-facing ones (FR-6.10): the export is the
    candidate's copy, and a private stage name would leak through it exactly as
    it once leaked through notifications. Individual recruiters are not named,
    for the same reason the "who opened your profile" screen does not name them.
    """
    from apps.applications.models import Application, CoverLetter
    from apps.applications.services import visibility
    from apps.messaging.models import Message, SenderKind

    from . import talent

    applications = (
        Application.objects.filter(candidate=profile)
        .select_related("job__company", "stage")
        .order_by("applied_at")
    )
    messages = (
        Message.objects.filter(conversation__candidate=profile)
        .select_related("conversation__company", "conversation__job")
        .order_by("created_at")
    )
    return {
        "applications": [
            {
                "job": row.job.title,
                "company": row.job.company.name,
                "applied_at": _when(row.applied_at),
                "stage": row.stage.public_label,
                "closed_reason": row.closed_reason or None,
                "withdrawn_at": _when(row.withdrawn_at),
                "answers": row.answers,
            }
            for row in applications
        ],
        "cover_letters": [
            {"title": row.title, "content": row.content, "created_at": _when(row.created_at)}
            for row in CoverLetter.objects.filter(profile=profile).order_by("created_at")
        ],
        "messages": [
            {
                "company": row.conversation.company.name,
                "about": job.title if (job := row.conversation.job) is not None else None,
                "from": "you" if row.sender_kind == SenderKind.CANDIDATE else "employer",
                "sent_at": _when(row.created_at),
                "body": None if row.deleted_at else row.body,
                "deleted": row.deleted_at is not None,
            }
            for row in messages
        ],
        "profile_views": [
            {
                "company": row["company"],
                "day": _date(row["day"]),
                "times": row["times"],
                "sources": list(row["sources"]),
            }
            for row in talent.views_for(profile, limit=10_000)
        ],
        "access": [
            {
                "company": grant.company.name,
                "reason": grant.get_basis_display(),
                "since": _when(grant.granted_at),
                "until": _when(grant.expires_at),
            }
            for grant in visibility.companies_with_access(profile)
        ],
    }


def _number(value: object) -> str | None:
    return str(value) if value is not None else None


def _when(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _date(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None
