"""
Parsing a claimed upload (SRS §5.5.2, ADR-A32).

Two preconditions, both refusals rather than warnings:

  * **Claimed.** An unclaimed upload is never read. This is the P4 exit
    criterion, and it is enforced here rather than assumed by callers.
  * **Scanned clean.** Reading a file the scanner has not cleared hands
    hostile input to a parser inside the worker.

Everything downstream of that is deterministic: same file, same engine version,
same output. No AI call exists in this path — `apps.resumes` does not import
`apps.ai`, and cannot, because it does not exist yet.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from django.db import transaction
from django.urls import reverse

from apps.audit import services as audit
from apps.common.errors import InvalidState
from apps.files.constants import ScanStatus
from apps.notifications import events

from ..models import ParseStatus, ResumeParse, ResumeUpload
from ..parsing import (
    ENGINE,
    ENGINE_VERSION,
    ExtractionFailed,
    UnsupportedFormat,
    extract_text,
    page_count,
)
from ..parsing import (
    parse as parse_text,
)
from ..parsing.base import STRUCTURED_SCHEMA_VERSION

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

logger = logging.getLogger(__name__)


def parse_upload(*, upload: ResumeUpload, actor: User | None = None) -> ResumeParse:
    """Read a claimed, clean upload into structured data.

    Not one transaction: reading the file and running the parser take real time
    and hold no locks worth holding, and a worker that keeps a write
    transaction open for the length of a PDF parse is a worker blocking
    everything else on that row.
    """
    _require_processable(upload)

    ResumeUpload.objects.filter(pk=upload.pk).update(parse_status=ParseStatus.RUNNING)
    started = time.perf_counter()  # monotonic ticks at ~16 ms on Windows; parses are faster

    try:
        # The upload already read this file once to validate it, and left the
        # text and page count behind; reading them again doubled the time a
        # PDF spent in pdfminer (docs/benchmarks/CV-Analysis-Timing.md).
        from django.core.cache import cache

        from .upload import handoff_key

        text = cache.get(handoff_key(upload.sha256)) if upload.sha256 else None
        data = None
        if text is None:
            data = _read(upload)
            text = extract_text(data, mime=upload.mime)
        parsed = parse_text(text, mime=upload.mime)
        pages = upload.pages
        if pages is None:
            data = data if data is not None else _read(upload)
            pages = page_count(data, mime=upload.mime)
    except UnsupportedFormat as exc:
        _fail(upload, ParseStatus.UNSUPPORTED, str(exc))
        raise InvalidState("That file format cannot be read.") from exc
    except ExtractionFailed as exc:
        _fail(upload, ParseStatus.FAILED, str(exc))
        raise InvalidState(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - user files are hostile input
        logger.exception("resume parse crashed for upload %s", upload.public_id)
        _fail(upload, ParseStatus.FAILED, "The file could not be read.")
        raise InvalidState("That file could not be read.") from exc

    duration_ms = int((time.perf_counter() - started) * 1000)

    with transaction.atomic():
        record, _created = ResumeParse.objects.update_or_create(
            upload=upload,
            defaults={
                "engine": ENGINE,
                "engine_version": ENGINE_VERSION,
                "raw_text": parsed.raw_text,
                "structured": parsed.as_structured(),
                "structured_schema_version": STRUCTURED_SCHEMA_VERSION,
                "confidence": {key: str(value) for key, value in parsed.confidence.items()},
                "warnings": list(parsed.warnings),
                "duration_ms": duration_ms,
            },
        )

        upload.parse_status = ParseStatus.PARSED
        upload.parse_error = ""
        upload.pages = pages
        upload.save(update_fields=["parse_status", "parse_error", "pages", "updated_at"])

        events.emit(
            "resume.parsed",
            upload,
            payload={
                "upload_public_id": str(upload.public_id),
                "engine_version": ENGINE_VERSION,
                # Counts, never content: the outbox has a different retention
                # from the parse and must not accumulate resume text.
                "skills_found": len(parsed.skill_terms),
                "experience_found": len(parsed.experience),
                # The owner, for the notice; a guest upload has none and
                # reaches nobody.
                "user_id": upload.profile.user_id if upload.profile is not None else None,
                "target_url": reverse("resume:upload") + "#import-proposals",
            },
        )
        audit.record(
            verb="resume.parsed",
            actor=actor,
            target=upload,
            metadata={"engine_version": ENGINE_VERSION, "duration_ms": duration_ms},
        )

    return record


def _require_processable(upload: ResumeUpload) -> None:
    if not upload.may_process:
        # ADR-A32. The single most important check in this app.
        raise InvalidState(
            "That upload has not been claimed by an account, so it may not be processed."
        )
    if upload.attachment.scan_status != ScanStatus.CLEAN:
        raise InvalidState(
            "That file has not been cleared by the virus scanner yet. It will be read "
            "automatically once it has."
        )
    if upload.attachment.deleted_at is not None:
        raise InvalidState("That file has been deleted.")


def _read(upload: ResumeUpload) -> bytes:
    from django.core.files.storage import storages

    storage = storages["private"]
    with storage.open(upload.attachment.storage_key, "rb") as handle:
        return handle.read()


def _fail(upload: ResumeUpload, status: str, message: str) -> None:
    ResumeUpload.objects.filter(pk=upload.pk).update(parse_status=status, parse_error=message[:255])
