"""
Rendering and export (SRS §5.5.2, FR-3.10, ADR-A8).

The rule this module exists to hold: **only a `ResumeVersion` is ever
rendered.** Not a draft, not a document, not "the current content". A draft
changes between the moment a file is produced and the moment an employer opens
it, so a PDF of a draft cannot be reproduced and cannot be proven.

`export()` is the candidate-facing entry point and does the two things the SRS
requires together — it **finalizes a version** (reason `exported`, the fourth
permitted act) and renders it. Because finalize reuses an unchanged version and
rendering is cached by (version, template, format), pressing Export twice
produces one version, one file, and one row.

The cache is the unique constraint. Rendering is a pure function of those three
columns *for a given engine version*, so a render written by an older engine is
replaced rather than served.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import TYPE_CHECKING, Any

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common import retention
from apps.common.errors import InvalidState
from apps.common.hashing import sha256_bytes
from apps.files import services as file_services
from apps.files.constants import Visibility
from apps.notifications import events

from ..models import (
    RenderFormat,
    ResumeDocument,
    ResumeRender,
    ResumeTemplate,
    ResumeVersion,
    VersionReason,
)
from ..rendering import RenderFailed, UnsupportedRenderFormat
from ..rendering import render as render_bytes
from ..rendering.base import stale_for
from . import versions as version_service

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

logger = logging.getLogger(__name__)

#: MIME per format, for the stored attachment. Taken from the renderer's own
#: result rather than guessed here, but declared so the accepted set is one
#: readable list.
ACCEPTED_MIMES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)


@transaction.atomic
def export(
    *,
    document: ResumeDocument,
    actor: User,
    fmt: str = RenderFormat.PDF,
    template: ResumeTemplate | None = None,
) -> ResumeRender:
    """Freeze the document and render it. The candidate-facing path.

    Finalizing here rather than in the view is deliberate: export is one of the
    four acts that may create a version (SRS §5.5.1), and a caller that
    rendered without finalizing would produce a file with no immutable record
    behind it.

    The plan decides which formats a candidate may take away (P18). This is the
    **candidate-facing** path only: `render_version` is untouched, because the
    render `submit_application` freezes is the platform keeping its own
    evidentiary record (ADR-A8) rather than a download somebody bought.
    """
    from apps.billing.services import entitlements

    entitlements.require(document.profile, "export_formats", value=fmt)

    version = version_service.finalize(
        document=document, actor=actor, reason=VersionReason.EXPORTED
    )
    return render_version(
        version=version, actor=actor, fmt=fmt, template=template or document.template
    )


def render_version(
    *,
    version: ResumeVersion,
    actor: User | None = None,
    fmt: str = RenderFormat.PDF,
    template: ResumeTemplate | None = None,
) -> ResumeRender:
    """Render one immutable version, reusing the cached file when there is one.

    Not one transaction end to end: rendering takes real CPU and holds no lock
    worth holding, and a worker keeping a write transaction open for the length
    of a PDF blocks everything else on that row.
    """
    if fmt not in RenderFormat.values:
        raise InvalidState(f"{fmt!r} is not a format this platform produces.")

    chosen = template or version.document.template
    cached = _cached(version, chosen, fmt)
    if cached is not None:
        return cached

    # Which template's *look* to draw in, as opposed to which text to draw.
    #
    # A caller that names a template is answering a question the candidate just
    # asked on screen: they picked Vertex in the gallery and pressed Export.
    # The version's own snapshot is the answer to a different question — "what
    # did this look like when it was submitted" — and it must keep winning when
    # nobody named a template, because an application's evidentiary render is
    # not allowed to drift with today's template row (ADR-A8).
    #
    # These were the same line until now, and the version won both times: every
    # export of an unchanged CV came back drawn in whichever template happened
    # to be selected the first time the version was frozen. The render row
    # recorded the template the candidate chose, so nothing in the data looked
    # wrong; only the file was.
    look = (
        chosen.snapshot()
        if template is not None
        else (version.template_snapshot or chosen.snapshot())
    )

    started = time.monotonic()
    try:
        result = render_bytes(version.content, template=look, fmt=fmt)
    except UnsupportedRenderFormat as exc:
        raise InvalidState(str(exc)) from exc
    except RenderFailed as exc:
        logger.exception("render failed for version %s as %s", version.pk, fmt)
        raise InvalidState(str(exc)) from exc

    if result.mime not in ACCEPTED_MIMES:
        raise InvalidState("The renderer produced an unexpected file type.")

    duration_ms = int((time.monotonic() - started) * 1000)
    digest = sha256_bytes(result.data)

    with transaction.atomic():
        attachment = file_services.store(
            uploaded_file=ContentFile(result.data, name=_filename(version, fmt)),
            visibility=Visibility.PRIVATE,
            accepted_types=ACCEPTED_MIMES,
            uploaded_by=actor,
            # Generated by this platform from content already scanned on the way
            # in. Queueing a scan for every export would double the file
            # subsystem's work to re-examine bytes it produced itself.
            scan=False,
            # A render is a cache of an immutable version, so it has an expiry
            # and the purge job honours it (§15.3, `retention.RENDER_DAYS`).
            # Without this the retention table named a rule that nothing
            # enforced: `purge_attachments` only removes what is soft-deleted or
            # expired, and nothing ever set an expiry on a render. It regenerates
            # on demand, so expiry costs a re-render and nothing else.
            expires_at=timezone.now() + dt.timedelta(days=retention.RENDER_DAYS),
        )
        file_services.mark_clean(attachment=attachment, reason="platform-generated render")

        try:
            record = ResumeRender.objects.create(
                version=version,
                template=chosen,
                format=fmt,
                attachment=attachment,
                sha256=digest,
                engine=result.engine,
                engine_version=result.engine_version,
                pages=result.pages,
                duration_ms=duration_ms,
                fonts=result.fonts,
            )
        except IntegrityError:
            # Two exports of one document raced. The constraint is the
            # arbitrator; the loser reads the winner's row rather than failing
            # a click the candidate is watching.
            existing = _cached(version, chosen, fmt, allow_stale=True)
            if existing is None:
                raise
            return existing

        # Inside the block, with the row it describes.
        #
        # These two calls used to sit after it, and `events.emit` refuses to
        # run without a transaction — so the first export of any version, and
        # therefore the first application submitted against a fresh CV, raised
        # instead of completing. The test suite never saw it because Django
        # wraps every `TestCase` in a transaction, which is exactly the
        # condition the guard is checking for.
        events.emit(
            "resume.rendered",
            record,
            payload={
                "document_public_id": str(version.document.public_id),
                "version_no": version.version_no,
                "format": fmt,
                "sha256": digest,
            },
        )
        audit.record(
            verb="resume.rendered",
            actor=actor,
            target=version,
            metadata={"format": fmt, "template": chosen.slug, "duration_ms": duration_ms},
        )

    return record


def _cached(
    version: ResumeVersion, template: ResumeTemplate, fmt: str, *, allow_stale: bool = False
) -> ResumeRender | None:
    """The stored render for this combination, if it is still usable.

    A render whose attachment has been purged, or which an older engine
    produced, is not a cache hit: serving it would hand back a file the current
    renderer would not produce.
    """
    record = (
        ResumeRender.objects.select_related("attachment", "version", "template")
        .filter(version=version, template=template, format=fmt)
        .first()
    )
    if record is None:
        return None

    if not allow_stale and stale_for(record.engine_version):
        record.delete()
        return None

    # Deleted *or* expired. `is_servable` covers both, and the difference
    # matters: a render past `expires_at` is refused by the file policy at
    # download, so returning it here would hand back a cache hit that 404s.
    if not record.attachment.is_servable:
        record.delete()
        return None

    return record


@transaction.atomic
def invalidate(*, version: ResumeVersion, actor: User | None = None) -> int:
    """Drop every cached render of one version.

    Used by redaction: the tombstone must not be readable as the original PDF
    sitting in object storage (ADR-A38).
    """
    removed = 0
    for record in version.renders.select_related("attachment"):
        file_services.soft_delete(attachment=record.attachment, actor=actor)
        record.delete()
        removed += 1

    if removed:
        audit.record(
            verb="resume.renders_invalidated",
            actor=actor,
            target=version,
            metadata={"removed": removed},
        )
    return removed


def _filename(version: ResumeVersion, fmt: str) -> str:
    return f"cv-v{version.version_no}.{fmt}"


@transaction.atomic
def purge_expired(*, now: Any = None) -> int:
    """Drop renders whose cached file has expired (§15.3, `RENDER_DAYS`).

    A render is a cache of an immutable version and regenerates on demand, so
    letting it expire costs a re-render and nothing else.

    This exists rather than letting the attachment purge handle it because
    `ResumeRender.attachment` is `PROTECT`: deleting the attachment underneath a
    surviving render row raises `ProtectedError` and would take the nightly file
    purge down with it. The owning app removes its row first and soft-deletes
    the file; `files.purge` then removes the bytes on its own schedule.
    """
    moment = now or timezone.now()
    removed = 0

    for record in ResumeRender.objects.select_related("attachment").filter(
        attachment__expires_at__lte=moment, attachment__deleted_at__isnull=True
    ):
        file_services.soft_delete(attachment=record.attachment)
        record.delete()
        removed += 1

    if removed:
        logger.info("purged expired renders", extra={"count": removed})
    return removed
