"""
Where a CV the candidate just handed over has got to, on every page (CVP-04).

Manual testing: after uploading, a candidate could open the progress page but
not leave it without losing track — nothing outside Uploads said a file was
still being read, and nothing said when it was done. This module answers one
question for the top-bar chip: "is a CV I uploaded still moving, and if not,
is there something new to look at?"

Only uploads this browser session asked to watch are looked at (the upload
answer adds them), so a candidate with nothing in flight pays no query at all,
and the pages under a query ceiling (NFR-2) are unchanged for everyone else.
A watched upload stops being watched once its result has been opened.

Steps, not a percentage. The work is a virus scan and then a read; neither
reports how far through it is, and "45%" would be a number made up to look
like progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SESSION_KEY = "cv_watch"
#: Upper bound on remembered uploads, so the session cannot grow for ever.
MAX_WATCHED = 5


@dataclass(frozen=True)
class Progress:
    state: str  # "scanning" | "reading" | "ready" | "failed"
    step: int
    steps: int
    filename: str
    upload_public_id: str

    @property
    def running(self) -> bool:
        return self.state in ("scanning", "reading")


def watch(request: Any, upload_public_id: str) -> None:
    """Follow this upload in the top bar until its result is opened."""
    watched = [value for value in request.session.get(SESSION_KEY, []) if value != upload_public_id]
    request.session[SESSION_KEY] = [upload_public_id, *watched][:MAX_WATCHED]


def forget(request: Any, upload_public_ids: list[str] | None = None) -> None:
    """Stop following finished uploads (or the given ones)."""
    watched = request.session.get(SESSION_KEY)
    if not watched:
        return
    if upload_public_ids is None:
        request.session.pop(SESSION_KEY, None)
        return
    kept = [value for value in watched if value not in upload_public_ids]
    if kept:
        request.session[SESSION_KEY] = kept
    else:
        request.session.pop(SESSION_KEY, None)


def current(request: Any) -> Progress | None:
    """The most recent watched upload's state, or None."""
    session = getattr(request, "session", None)
    if session is None:
        return None
    watched = session.get(SESSION_KEY)
    user = getattr(request, "user", None)
    if not watched or not getattr(user, "is_authenticated", False):
        return None

    from apps.files.constants import ScanStatus

    from .models import ParseStatus, ResumeUpload

    uploads = {
        str(upload.public_id): upload
        for upload in ResumeUpload.objects.filter(
            public_id__in=watched, profile__user=user
        ).select_related("attachment")
    }
    for public_id in watched:
        upload = uploads.get(public_id)
        if upload is None:
            continue
        scan = upload.attachment.scan_status
        if scan == ScanStatus.PENDING:
            state, step = "scanning", 1
        elif scan != ScanStatus.CLEAN or upload.parse_status in (
            ParseStatus.FAILED,
            ParseStatus.UNSUPPORTED,
        ):
            state, step = "failed", 3
        elif upload.parse_status == ParseStatus.PARSED:
            state, step = "ready", 3
        else:
            state, step = "reading", 2
        return Progress(
            state=state,
            step=step,
            steps=3,
            filename=upload.original_filename,
            upload_public_id=public_id,
        )
    return None
