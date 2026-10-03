"""
Scheduled maintenance commands.

Every scheduled job is a management command so an operator can run it by hand
during an incident (DEVELOPER.md §6.5). That only holds if they actually run,
so each one has a test.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import IdempotencyKey
from apps.accounts.tests.factories import create_user
from apps.common import idempotency
from apps.files import services as file_services
from apps.files.constants import DOCUMENT_TYPES
from apps.files.models import Attachment
from apps.files.scanning import reset_scanner_cache
from apps.files.tests import factories
from apps.notifications import events
from apps.notifications.models import OutboxEvent
from apps.notifications.services import outbox


def _run(name: str, *args: str) -> str:
    out = StringIO()
    call_command(name, *args, stdout=out)
    return out.getvalue().strip()


class PurgeIdempotencyKeysTests(TestCase):
    def test_removes_expired_keys_only(self):
        user = create_user()
        idempotency.claim(key="live", endpoint="e", request_hash="h", user=user)
        idempotency.claim(key="dead", endpoint="e", request_hash="h", user=user)
        IdempotencyKey.objects.filter(key="dead").update(
            expires_at=timezone.now() - timedelta(hours=1)
        )

        output = _run("purge_idempotency_keys")

        self.assertEqual(output, "deleted 1")
        self.assertTrue(IdempotencyKey.objects.filter(key="live").exists())


class PurgeOutboxTests(TestCase):
    def setUp(self):
        self.attachment = Attachment.objects.create(
            storage_key="ab/cd/x.pdf",
            filename="x.pdf",
            mime="application/pdf",
            size=1,
            sha256="c" * 64,
        )

    def _emit(self):
        return events.emit(
            "file.scan_failed",
            self.attachment,
            payload={
                "attachment_public_id": str(self.attachment.public_id),
                "filename": "x.pdf",
                "scan_status": "infected",
            },
        )

    def test_deletes_delivered_events_past_retention(self):
        self._emit()
        outbox.drain()
        OutboxEvent.objects.update(processed_at=timezone.now() - timedelta(hours=48))

        self.assertEqual(_run("purge_outbox", "--hours", "24"), "deleted 1")
        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_keeps_undelivered_events(self):
        self._emit()
        self.assertEqual(_run("purge_outbox", "--hours", "0"), "deleted 0")
        self.assertEqual(OutboxEvent.objects.count(), 1)


class OutboxWorkerCommandTests(TestCase):
    def test_once_drains_a_single_batch_and_exits(self):
        attachment = Attachment.objects.create(
            storage_key="ab/cd/y.pdf",
            filename="y.pdf",
            mime="application/pdf",
            size=1,
            sha256="d" * 64,
        )
        events.emit(
            "file.scan_failed",
            attachment,
            payload={
                "attachment_public_id": str(attachment.public_id),
                "filename": "y.pdf",
                "scan_status": "error",
            },
        )

        self.assertEqual(_run("outbox_worker", "--once"), "drained 1")
        self.assertIsNotNone(OutboxEvent.objects.get().processed_at)


class PurgeAttachmentsTests(TestCase):
    def setUp(self):
        reset_scanner_cache()
        self.addCleanup(reset_scanner_cache)
        self.user = create_user()

    def _stored(self):
        return file_services.store(
            uploaded_file=factories.pdf_upload(),
            accepted_types=DOCUMENT_TYPES,
            uploaded_by=self.user,
            scan=False,
        )

    def test_dry_run_reports_without_deleting(self):
        attachment = self._stored()
        file_services.soft_delete(attachment=attachment, actor=self.user)

        self.assertEqual(_run("purge_attachments", "--dry-run"), "would purge 1")
        self.assertEqual(Attachment.objects.count(), 1)

    def test_purges_soft_deleted_attachments(self):
        attachment = self._stored()
        file_services.soft_delete(attachment=attachment, actor=self.user)

        self.assertEqual(_run("purge_attachments"), "purged 1")
        self.assertEqual(Attachment.objects.count(), 0)
