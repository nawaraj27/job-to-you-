"""
Background work for files (DEVELOPER.md §3.9).

Tasks take identifiers, not model instances; hold no business logic; declare
their queue; and are safe to run twice.
"""

from __future__ import annotations

import logging

from celery import shared_task

from . import services
from .models import Attachment

logger = logging.getLogger(__name__)


@shared_task(
    queue="files",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def scan_attachment(self, attachment_id: int) -> str:
    """Scan one stored attachment.

    Idempotent: rescanning a file produces the same verdict, and a file that
    has vanished is not an error worth retrying.
    """
    attachment = Attachment.objects.filter(pk=attachment_id).first()
    if attachment is None:
        logger.info("scan skipped, attachment gone", extra={"attachment_id": attachment_id})
        return "missing"

    scanned = services.scan_attachment(attachment)
    return scanned.scan_status


@shared_task(queue="files")
def purge_attachments() -> int:
    """Remove bytes for deleted and expired attachments. Scheduled daily."""
    return services.purge()


#: How long a scan may sit "pending" before it is asked for again.
STALLED_SCAN_MINUTES = 15


@shared_task(queue="files")
def requeue_stalled_scans() -> int:
    """Ask again for scans whose task never ran (STALL-01).

    A scan is a queued task. A broker restarted before a worker took it loses
    it, and nothing asked again: the file stayed "pending" for good — a CV
    that was never read, a certificate staff could never open. Scanning is
    idempotent, so asking twice costs one extra scan at worst.
    """
    import datetime as dt

    from django.utils import timezone

    from .constants import ScanStatus

    cutoff = timezone.now() - dt.timedelta(minutes=STALLED_SCAN_MINUTES)
    stalled = list(
        Attachment.objects.filter(
            scan_status=ScanStatus.PENDING, deleted_at__isnull=True, updated_at__lt=cutoff
        ).values_list("pk", flat=True)[:500]
    )
    for attachment_id in stalled:
        scan_attachment.delay(attachment_id)
    return len(stalled)
