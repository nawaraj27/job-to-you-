"""
The only way to read a private file (SRS §14.2).

Order matters and is the point of the view:

    token -> attachment -> policy -> servable -> log -> bytes

The token proves a link was issued. The policy decides whether this actor may
read it now, because a permission can change after a link is minted. Nothing is
served before the access is recorded, so the log cannot miss a read that
happened.

Responses are always `Content-Disposition: attachment`. A user-supplied PDF or
document rendered inline executes in this origin, which is script execution
with a user's session attached (DEVELOPER.md §6.4).
"""

from __future__ import annotations

import logging

from django.core.files.storage import storages
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
)
from django.http.response import HttpResponseBase
from django.views import View

from apps.common.errors import DomainError, NotFound

from . import policies, services, signing
from .constants import Visibility
from .models import Attachment

logger = logging.getLogger(__name__)


class FileAccessView(View):
    """GET /files/<public_id>/?t=<token>"""

    def get(self, request: HttpRequest, public_id: str) -> HttpResponseBase:
        try:
            return self._serve(request, public_id)
        except DomainError as exc:
            # Every failure returns the same shape. Distinguishing "expired"
            # from "not yours" from "does not exist" hands an attacker a probe.
            return HttpResponse(exc.message, status=exc.status_code, content_type="text/plain")

    def _serve(self, request: HttpRequest, public_id: str) -> HttpResponseBase:
        attachment = Attachment.objects.alive().filter(public_id=public_id).first()
        if attachment is None:
            raise NotFound("That file does not exist, or you cannot open it.")

        purpose = ""
        if attachment.visibility != Visibility.PUBLIC:
            token = request.GET.get("t", "")
            if not token:
                raise NotFound("That file does not exist, or you cannot open it.")
            payload = signing.verify(token, expected_public_id=public_id)
            purpose = payload.get("pur", "")

        actor = request.user
        policies.require_can_access(actor, attachment)

        # Normalised at the edge: a foreign key cannot hold an AnonymousUser,
        # so services take a real user or None and never both.
        recorded_actor = actor if actor.is_authenticated else None

        services.record_access(
            attachment=attachment,
            actor=recorded_actor,
            purpose=purpose,
            ip=_client_ip(request),
            request_id=getattr(request, "request_id", ""),
        )

        return _respond_with_bytes(attachment)


def serve(attachment: Attachment) -> HttpResponseBase:
    """Bytes for a file whose access a purpose-built screen has already decided.

    For surfaces that authorise by their own rule rather than by token — the
    staff verification review — and that have logged the read with
    `services.record_access` before calling this.
    """
    return _respond_with_bytes(attachment)


def _respond_with_bytes(attachment: Attachment) -> HttpResponseBase:
    """Redirect to a backend-signed URL when there is one, otherwise stream.

    An object store can sign a short-lived URL itself, which keeps large files
    off the web tier. A filesystem backend cannot, so the file is streamed —
    correct locally, and never the production path.
    """
    storage = storages[attachment.storage_alias]

    signed_url = _backend_signed_url(storage, attachment.storage_key)
    if signed_url is not None:
        return HttpResponseRedirect(signed_url)

    try:
        stream = storage.open(attachment.storage_key, "rb")
    except OSError:
        # The row says the file exists and the store does not have it. That is
        # an operations problem to page someone about, not a 500 for the reader
        # (found by the link sweep, 2026-09-18, on the staff document route).
        logger.error(
            "attachment bytes missing from storage",
            extra={"attachment": str(attachment.public_id), "storage": attachment.storage_alias},
        )
        raise Http404("That file is not available.") from None

    response = FileResponse(
        stream,
        as_attachment=True,
        filename=attachment.filename,
        content_type=attachment.mime,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


def _backend_signed_url(storage, key: str) -> str | None:
    """A URL only if the backend can produce an expiring one of its own.

    FileSystemStorage raises when it has no base_url, which is exactly how the
    private backend is configured — so this returns None and the caller streams.
    """
    try:
        url = storage.url(key)
    except (ValueError, NotImplementedError):
        return None
    return url if url.startswith(("http://", "https://")) else None


def _client_ip(request: HttpRequest) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None
