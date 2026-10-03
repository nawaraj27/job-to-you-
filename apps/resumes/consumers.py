"""
What ingestion does when something happens elsewhere (DEVELOPER.md §4.5).

One subscriber: a file finishing its virus scan is the signal that a claimed
upload may now be read. Subscribing rather than polling means the wait costs
nothing and the ordering guarantee comes from the outbox rather than from a
timer someone tuned once.
"""

from __future__ import annotations

import logging

from apps.files.constants import ScanStatus
from apps.notifications.consumers import subscribe
from apps.notifications.models import OutboxEvent

logger = logging.getLogger(__name__)


@subscribe("file.scanned")
def parse_when_clean(event: OutboxEvent) -> None:
    """Queue parsing for any claimed upload of a file that just came back clean."""
    payload = event.payload or {}
    if payload.get("scan_status") != ScanStatus.CLEAN:
        return

    from .models import ParseStatus, ResumeUpload
    from .tasks import parse_upload

    uploads = ResumeUpload.objects.owned().filter(
        attachment__public_id=payload.get("attachment_public_id"),
        parse_status=ParseStatus.PENDING,
    )
    for upload_id in uploads.values_list("pk", flat=True):
        parse_upload.delay(upload_id)
