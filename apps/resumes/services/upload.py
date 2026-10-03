"""
Taking a CV in (SRS §5.5.2, ADR-A32).

Three things happen here and nothing else: the file is stored through
`apps.files` (which sniffs, size-limits and queues a virus scan), an upload row
is created, and — if an account owns it — parsing is queued behind the scan.

**A guest upload is stored and scanned and nothing else.** It gets a token, no
profile, and `parse_status=blocked`. Nothing reads its contents until an
account claims it. That is ADR-A32, and it is the difference between holding a
file someone handed us and processing a stranger's personal data.

Deduplication is by content hash per profile: uploading the same CV twice is
one upload, because the second row would produce a second parse, a second set
of proposals and a duplicate in every list the candidate looks at.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.core.files import File
from django.db import transaction

from apps.audit import services as audit
from apps.common.errors import InvalidState
from apps.common.hashing import sha256_stream
from apps.files import services as file_services
from apps.files.constants import CV_TYPES, ScanStatus, Visibility
from apps.notifications import events

from ..models import ParseStatus, ResumeUpload
from . import validation

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile

#: How long an unclaimed upload survives (SRS §5.5.2). Long enough to register
#: and come back, short enough that the platform is not a warehouse of files
#: belonging to people it cannot identify.
GUEST_RETENTION_HOURS = 24

#: What a CV may be: PDF, Word 97-2003 and Word. Images are excluded
#: deliberately: a photo of a CV cannot be parsed, and accepting one promises
#: something the product cannot deliver. `services.validation` decides the rest.
ACCEPTED_TYPES = CV_TYPES

#: How long the text read during validation is kept for the parse to reuse.
#: The parse normally runs seconds later, once the scan is clean.
TEXT_HANDOFF_SECONDS = 15 * 60


def handoff_key(digest: str) -> str:
    from ..parsing import ENGINE_VERSION

    return f"resume:text:{ENGINE_VERSION}:{digest}"


@transaction.atomic
def upload_for_profile(
    *, profile: CandidateProfile, actor: User, uploaded_file: File
) -> tuple[ResumeUpload, bool]:
    """Store a CV for a signed-in candidate. Returns (upload, created)."""
    # Refused here, before anything is stored: a rejected file leaves no
    # attachment, no upload row and no job behind it. Before the duplicate
    # check too, so the same bytes under a wrong name are still refused.
    checked = validation.check(uploaded_file, read_content=True)
    digest = _digest(uploaded_file)

    existing = ResumeUpload.objects.filter(profile=profile, sha256=digest).first()
    if existing is not None:
        # The same bytes again. Returned rather than refused: re-uploading is
        # usually someone checking it worked, and an error would read as one.
        return existing, False

    attachment = file_services.store(
        uploaded_file=uploaded_file,
        visibility=Visibility.PRIVATE,
        accepted_types=ACCEPTED_TYPES,
        uploaded_by=actor,
    )

    upload = ResumeUpload.objects.create(
        profile=profile,
        attachment=attachment,
        original_filename=attachment.filename,
        mime=attachment.mime,
        size=attachment.size,
        sha256=digest,
        parse_status=ParseStatus.PENDING,
        claimed_at=attachment.created_at,
        pages=checked.pages,
    )
    if checked.text is not None:
        # The parse reads this instead of extracting the file a second time.
        cache.set(handoff_key(digest), checked.text, TEXT_HANDOFF_SECONDS)

    events.emit(
        "resume.uploaded",
        upload,
        payload={
            "upload_public_id": str(upload.public_id),
            "profile_public_id": str(profile.public_id),
            "mime": upload.mime,
        },
    )
    audit.record(
        verb="resume.uploaded",
        actor=actor,
        target=upload,
        metadata={"filename": upload.original_filename, "size": upload.size},
    )

    _queue_parse_when_clean(upload)
    return upload, True


@transaction.atomic
def upload_as_guest(*, uploaded_file: File) -> tuple[ResumeUpload, str]:
    """Store a CV for someone with no account. Returns (upload, token).

    The token is returned once and stored hashed, like any other bearer
    credential: it is the only thing standing between an anonymous URL and
    somebody else's CV.
    """
    # Structure only: ADR-A32 forbids reading a guest's content before claim.
    checked = validation.check(uploaded_file, read_content=False)
    attachment = file_services.store(
        uploaded_file=uploaded_file,
        visibility=Visibility.PRIVATE,
        accepted_types=ACCEPTED_TYPES,
        uploaded_by=None,
    )

    token = secrets.token_urlsafe(32)
    upload = ResumeUpload.objects.create(
        profile=None,
        guest_token=_hash_token(token),
        attachment=attachment,
        original_filename=attachment.filename,
        mime=attachment.mime,
        size=attachment.size,
        sha256=_digest(uploaded_file),
        # Blocked, not pending: nothing may read this file's contents until an
        # account claims it (ADR-A32).
        parse_status=ParseStatus.BLOCKED,
        pages=checked.pages,
    )

    events.emit(
        "resume.guest_uploaded",
        upload,
        payload={"upload_public_id": str(upload.public_id), "mime": upload.mime},
    )
    return upload, token


def find_guest_upload(token: str) -> ResumeUpload | None:
    """The unclaimed upload this token opens, if it is still there."""
    if not token:
        return None
    return ResumeUpload.objects.unclaimed().filter(guest_token=_hash_token(token)).first()


@transaction.atomic
def delete_upload(*, upload: ResumeUpload, actor: User) -> None:
    """Remove a CV the candidate no longer wants us to hold."""
    if upload.profile_id is None:
        raise InvalidState("That upload is not yours.")

    attachment = upload.attachment
    filename = upload.original_filename
    upload.delete()
    file_services.soft_delete(attachment=attachment, actor=actor)

    audit.record(verb="resume.upload_deleted", actor=actor, metadata={"filename": filename})


def _queue_parse_when_clean(upload: ResumeUpload) -> None:
    """Parse after commit, and only if the scanner has already cleared it.

    Most uploads are still `pending` at this point, and the scan task queues
    the parse itself when it finishes. This covers the case where the same
    bytes were scanned for an earlier upload and the attachment is already
    clean — content-addressed storage makes that common, not exotic.
    """
    if upload.attachment.scan_status != ScanStatus.CLEAN:
        return

    upload_id = upload.pk
    transaction.on_commit(lambda: _enqueue(upload_id))


def _enqueue(upload_id: int) -> None:
    from ..tasks import parse_upload

    parse_upload.delay(upload_id)


def _digest(uploaded_file: File) -> str:
    stream = uploaded_file.file
    if stream is None:
        raise InvalidState("That file could not be read.")
    return sha256_stream(stream)


def _hash_token(token: str) -> str:
    """Stored hashed, like any bearer credential.

    A database leak that includes guest tokens is a leak of every unclaimed CV;
    a leak of their hashes is not.
    """
    from apps.common.hashing import sha256_bytes

    return sha256_bytes(token.encode("utf-8"))
