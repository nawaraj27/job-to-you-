"""
File services (SRS §14.2).

Storing a file is four steps that must happen in this order and no other:
validate the bytes, write them, record the row, then scan. Scanning is last
because it needs the stored object, and it is asynchronous because it can be
slow — but nothing downstream may touch a file until it reports clean, which is
what `Attachment.is_servable` enforces at every read.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import IO, TYPE_CHECKING

from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import storages
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import DomainError, QuotaExceeded
from apps.common.hashing import sha256_stream
from apps.notifications import events

from . import sniff
from .constants import EXTENSIONS, ScanStatus, Visibility
from .models import Attachment, AttachmentAccess, AttachmentGrant
from .scanning import get_scanner

if TYPE_CHECKING:
    from apps.accounts.models import User

logger = logging.getLogger(__name__)

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_LENGTH = 120


class UnsupportedFileType(DomainError):
    status_code = 415
    code = "unsupported_file_type"
    default_message = "That file type is not accepted."


class FileTooLarge(QuotaExceeded):
    code = "file_too_large"
    default_message = "That file is larger than the limit."


def sanitize_filename(name: str) -> str:
    """Strip a filename to something safe to store, log and echo back.

    Never used to build a storage path — keys are content-addressed — but it is
    displayed to users and written into headers, so path separators, control
    characters and leading dots all have to go.
    """
    name = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    stem, _, suffix = name.rpartition(".")
    stem = _UNSAFE_NAME.sub("-", stem or name).strip("-.") or "file"
    suffix = _UNSAFE_NAME.sub("", suffix)[:10].lower()
    stem = stem[:_MAX_NAME_LENGTH]
    return f"{stem}.{suffix}" if suffix else stem


def storage_key_for(digest: str, media_type: str) -> str:
    """Content-addressed key: identical bytes occupy one object.

    Two levels of fan-out keep any single directory small on a filesystem
    backend and distribute keys evenly on an object store.
    """
    extension = EXTENSIONS.get(media_type, "")
    return f"{digest[:2]}/{digest[2:4]}/{digest}{extension}"


@transaction.atomic
def store(
    *,
    uploaded_file: File,
    visibility: str = Visibility.PRIVATE.value,
    accepted_types: frozenset[str],
    uploaded_by: User | None = None,
    expires_at: datetime | None = None,
    scan: bool = True,
) -> Attachment:
    """Validate, store and register a file. Scanning is queued, not inline.

    `accepted_types` is required rather than defaulted: every caller knows what
    it is willing to receive, and a default would eventually accept something a
    caller never intended.
    """
    size = uploaded_file.size or 0
    if size <= 0:
        raise UnsupportedFileType("That file is empty.")
    if size > settings.FILES_MAX_UPLOAD_BYTES:
        raise FileTooLarge(
            f"That file is larger than the {settings.FILES_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
        )

    stream: IO[bytes] | None = uploaded_file.file
    if stream is None:
        raise UnsupportedFileType("That file could not be read.")

    media_type = sniff.detect(stream)
    if media_type is None or media_type not in accepted_types:
        raise UnsupportedFileType()

    filename = sanitize_filename(uploaded_file.name or "")
    if not sniff.extension_matches(filename, media_type):
        raise UnsupportedFileType(
            "The file's name does not match its contents. Rename it and try again."
        )

    digest = sha256_stream(stream)
    key = storage_key_for(digest, media_type)

    storage_alias = "default" if visibility == Visibility.PUBLIC else "private"
    storage = storages[storage_alias]
    if not storage.exists(key):
        stream.seek(0)
        storage.save(key, File(stream))

    attachment = Attachment.objects.create(
        storage_key=key,
        filename=filename,
        mime=media_type,
        size=size,
        sha256=digest,
        visibility=visibility,
        uploaded_by=uploaded_by,
        expires_at=expires_at,
        scan_status=ScanStatus.PENDING,
    )

    if scan:
        attachment_id = attachment.id
        transaction.on_commit(lambda: _enqueue_scan(attachment_id))

    return attachment


def _enqueue_scan(attachment_id: int) -> None:
    from .tasks import scan_attachment

    scan_attachment.delay(attachment_id)


def scan_attachment(attachment: Attachment) -> Attachment:
    """Run the configured scanner and record the outcome.

    An unscannable file is recorded as `error`, not `clean`. Both non-clean
    outcomes are quarantined: `is_servable` is false for anything that is not
    explicitly clean, so a scanner outage degrades to files that cannot be
    opened rather than files that are trusted blindly.
    """
    storage = storages[attachment.storage_alias]

    try:
        with storage.open(attachment.storage_key, "rb") as handle:
            result = get_scanner().scan(handle)
    except FileNotFoundError:
        result = None

    if result is None:
        attachment.scan_status = ScanStatus.ERROR
        attachment.scan_error = "stored object missing"
    elif result.clean:
        attachment.scan_status = ScanStatus.CLEAN
    elif result.errored:
        attachment.scan_status = ScanStatus.ERROR
        attachment.scan_error = result.error[:255]
    else:
        attachment.scan_status = ScanStatus.INFECTED
        attachment.scan_signature = result.signature[:255]

    attachment.scanned_at = timezone.now()
    attachment.save(
        update_fields=[
            "scan_status",
            "scan_signature",
            "scan_error",
            "scanned_at",
            "updated_at",
        ]
    )

    _announce_scan(attachment)

    if attachment.scan_status != ScanStatus.CLEAN:
        _quarantine(attachment)

    return attachment


@transaction.atomic
def _announce_scan(attachment: Attachment) -> None:
    """Tell the rest of the system the verdict, whatever it is.

    Emitted for clean files too: a domain waiting to process an upload needs a
    signal, and the alternative is every domain polling the attachment table.
    """
    events.emit(
        "file.scanned",
        attachment,
        payload={
            "attachment_public_id": str(attachment.public_id),
            "scan_status": attachment.scan_status,
        },
    )


@transaction.atomic
def _quarantine(attachment: Attachment) -> None:
    """Remove the bytes and tell the rest of the system.

    The row survives so the event, the audit trail and the uploader's error
    message all have something to point at.
    """
    storage = storages[attachment.storage_alias]
    shared = (
        Attachment.objects.alive()
        .filter(storage_key=attachment.storage_key)
        .exclude(pk=attachment.pk)
        .exists()
    )
    if not shared:
        storage.delete(attachment.storage_key)

    audit.record(
        verb="attachment.quarantined",
        actor=attachment.uploaded_by,
        target=attachment,
        metadata={
            "scan_status": attachment.scan_status,
            "signature": attachment.scan_signature,
            "error": attachment.scan_error,
        },
    )
    events.emit(
        "file.scan_failed",
        attachment,
        payload={
            "attachment_public_id": str(attachment.public_id),
            "filename": attachment.filename,
            "scan_status": attachment.scan_status,
            "uploaded_by_id": attachment.uploaded_by_id,
        },
    )


@transaction.atomic
def mark_clean(*, attachment: Attachment, reason: str) -> Attachment:
    """Record that a file needs no scan because this platform produced it.

    Only for bytes generated server-side — a rendered PDF, an export — where
    the input was already scanned on the way in. Never for anything a user
    supplied: `scan_status` is what `is_servable` reads, and setting it by hand
    on an upload would serve an unscanned file.

    The reason is audited, so "why is this file clean without a scan" has an
    answer in the log rather than in someone's memory.
    """
    attachment.scan_status = ScanStatus.CLEAN
    attachment.scan_error = ""
    attachment.scanned_at = timezone.now()
    attachment.save(update_fields=["scan_status", "scan_error", "scanned_at", "updated_at"])

    audit.record(
        verb="attachment.marked_clean",
        actor=attachment.uploaded_by,
        target=attachment,
        metadata={"reason": reason[:200]},
    )
    return attachment


def share_with(
    *,
    attachment: Attachment,
    user: User,
    reason: str,
    expires_at: datetime | None = None,
) -> AttachmentGrant:
    """Let one more person read one private file (ADR-A52).

    Idempotent per reason, so re-sending the same file to the same person does
    not accumulate rows. The granting domain owns `reason`; this layer never
    interprets it.
    """
    grant, _ = AttachmentGrant.objects.get_or_create(
        attachment=attachment,
        user=user,
        reason=reason[:60],
        defaults={"expires_at": expires_at},
    )
    return grant


def revoke_share(*, attachment: Attachment, user: User, reason: str) -> int:
    """Withdraw one reason. Other reasons to read the file are untouched."""
    from django.utils import timezone as tz

    return AttachmentGrant.objects.filter(
        attachment=attachment, user=user, reason=reason[:60], revoked_at__isnull=True
    ).update(revoked_at=tz.now())


def record_access(
    *,
    attachment: Attachment,
    actor: User | None = None,
    purpose: str = "",
    ip: str | None = None,
    request_id: str = "",
) -> AttachmentAccess:
    """Log a read. Called on every served private file, without exception."""
    return AttachmentAccess.objects.create(
        attachment=attachment,
        actor=actor,
        purpose=purpose[:64],
        ip=ip,
        request_id=request_id[:64],
    )


@transaction.atomic
def soft_delete(*, attachment: Attachment, actor: User | None = None) -> Attachment:
    """Mark an attachment deleted. Bytes are removed by the purge job."""
    if attachment.deleted_at is None:
        attachment.deleted_at = timezone.now()
        attachment.save(update_fields=["deleted_at", "updated_at"])
        audit.record(verb="attachment.deleted", actor=actor, target=attachment)
    return attachment


def purge(*, older_than: datetime | None = None) -> int:
    """Delete bytes for soft-deleted and expired attachments.

    Storage is only freed when no surviving attachment shares the key, because
    content-addressed storage means two rows can point at one object.
    """
    cutoff = older_than or timezone.now()
    doomed = list(
        Attachment.objects.filter(deleted_at__lte=cutoff)
        | Attachment.objects.alive().filter(expires_at__lte=cutoff)
    )

    purged = 0
    for attachment in doomed:
        still_referenced = (
            Attachment.objects.filter(storage_key=attachment.storage_key)
            .exclude(pk=attachment.pk)
            .filter(deleted_at__isnull=True)
            .exists()
        )
        if not still_referenced:
            storages[attachment.storage_alias].delete(attachment.storage_key)

        try:
            attachment.delete()
        except ProtectedError:
            # Something still points at it with PROTECT — a resume render, say.
            # The owning app removes its row on its own schedule; this job must
            # not take the whole nightly purge down over one of them.
            logger.warning(
                "attachment still referenced, left for its owner to release",
                extra={"attachment_id": attachment.pk},
            )
            continue
        purged += 1

    if purged:
        logger.info("purged attachments", extra={"count": purged})
    return purged
