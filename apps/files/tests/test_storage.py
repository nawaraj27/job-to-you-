"""Type detection, storing, scanning and quarantine."""

from __future__ import annotations

from django.core.files.storage import storages
from django.test import TestCase, override_settings

from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.common.errors import QuotaExceeded
from apps.files import services, sniff
from apps.files.constants import DOCUMENT_TYPES, PDF, ScanStatus, Visibility
from apps.files.models import Attachment
from apps.files.scanning import reset_scanner_cache
from apps.files.tests import factories
from apps.notifications.models import OutboxEvent


class SniffTests(TestCase):
    def test_detects_pdf(self):
        import io

        self.assertEqual(sniff.detect(io.BytesIO(factories.MINIMAL_PDF)), PDF)

    def test_detects_docx_by_opening_the_container(self):
        import io

        from apps.files.constants import DOCX

        self.assertEqual(sniff.detect(io.BytesIO(factories.docx_bytes())), DOCX)

    def test_a_plain_zip_is_not_a_docx(self):
        # Shares the magic number. Extension-based detection would accept it.
        import io

        self.assertIsNone(sniff.detect(io.BytesIO(factories.zip_but_not_docx_bytes())))

    def test_unknown_content_is_rejected(self):
        import io

        self.assertIsNone(sniff.detect(io.BytesIO(b"just some text")))

    def test_extension_must_agree_with_content(self):
        self.assertTrue(sniff.extension_matches("cv.pdf", PDF))
        self.assertFalse(sniff.extension_matches("cv.docx", PDF))
        self.assertFalse(sniff.extension_matches("cv", PDF))


class StoreTests(TestCase):
    def setUp(self):
        self.user = create_user()
        reset_scanner_cache()
        self.addCleanup(reset_scanner_cache)

    def _store(self, upload=None, **kwargs):
        kwargs.setdefault("accepted_types", DOCUMENT_TYPES)
        kwargs.setdefault("uploaded_by", self.user)
        return services.store(uploaded_file=upload or factories.pdf_upload(), **kwargs)

    def test_stores_a_pdf_and_records_its_digest(self):
        attachment = self._store()

        self.assertEqual(attachment.mime, PDF)
        self.assertEqual(attachment.visibility, Visibility.PRIVATE)
        self.assertEqual(len(attachment.sha256), 64)
        self.assertTrue(storages["private"].exists(attachment.storage_key))

    def test_declared_content_type_is_ignored(self):
        # The browser says PDF; the bytes are a plain ZIP. Trusting the header
        # is how a parser ends up reading something it cannot handle.
        hostile = factories.upload(factories.zip_but_not_docx_bytes(), "cv.pdf", "application/pdf")
        with self.assertRaises(services.UnsupportedFileType):
            self._store(hostile)

    def test_extension_mismatch_is_rejected(self):
        with self.assertRaises(services.UnsupportedFileType):
            self._store(factories.upload(factories.MINIMAL_PDF, "cv.docx", "application/pdf"))

    def test_type_outside_the_caller_s_accepted_set_is_rejected(self):
        from apps.files.constants import IMAGE_TYPES

        with self.assertRaises(services.UnsupportedFileType):
            self._store(accepted_types=IMAGE_TYPES)

    def test_empty_file_is_rejected(self):
        with self.assertRaises(services.UnsupportedFileType):
            self._store(factories.upload(b"", "cv.pdf"))

    @override_settings(FILES_MAX_UPLOAD_BYTES=10)
    def test_oversized_file_is_rejected(self):
        with self.assertRaises(QuotaExceeded):
            self._store()

    def test_identical_content_is_stored_once(self):
        first = self._store()
        second = self._store()

        self.assertEqual(first.storage_key, second.storage_key)
        self.assertNotEqual(first.pk, second.pk)

    def test_filename_is_sanitised(self):
        attachment = self._store(
            factories.upload(factories.MINIMAL_PDF, "../../etc/pa ssword;.pdf")
        )
        self.assertNotIn("/", attachment.filename)
        self.assertNotIn("..", attachment.filename)
        self.assertTrue(attachment.filename.endswith(".pdf"))

    def test_a_new_upload_is_not_servable_until_scanned(self):
        attachment = self._store(scan=False)
        self.assertEqual(attachment.scan_status, ScanStatus.PENDING)
        self.assertFalse(attachment.is_servable)


class ScanTests(TestCase):
    def setUp(self):
        self.user = create_user()
        reset_scanner_cache()
        self.addCleanup(reset_scanner_cache)

    def _store(self, scan=False):
        return services.store(
            uploaded_file=factories.pdf_upload(),
            accepted_types=DOCUMENT_TYPES,
            uploaded_by=self.user,
            scan=scan,
        )

    def test_clean_scan_makes_the_file_servable(self):
        attachment = services.scan_attachment(self._store())

        self.assertEqual(attachment.scan_status, ScanStatus.CLEAN)
        self.assertTrue(attachment.is_servable)
        self.assertIsNotNone(attachment.scanned_at)

    @override_settings(FILES_SCANNER="apps.files.scanning.InfectedScanner")
    def test_infected_file_is_quarantined(self):
        reset_scanner_cache()
        attachment = self._store()
        key = attachment.storage_key

        attachment = services.scan_attachment(attachment)

        self.assertEqual(attachment.scan_status, ScanStatus.INFECTED)
        self.assertFalse(attachment.is_servable)
        self.assertFalse(storages["private"].exists(key))

    @override_settings(FILES_SCANNER="apps.files.scanning.InfectedScanner")
    def test_quarantine_is_audited_and_emits_an_event(self):
        reset_scanner_cache()
        services.scan_attachment(self._store())

        self.assertTrue(AuditLog.objects.filter(verb="attachment.quarantined").exists())
        event = OutboxEvent.objects.get(type="file.scan_failed")
        self.assertEqual(event.payload["scan_status"], ScanStatus.INFECTED)

    def test_an_unscannable_file_is_treated_as_unsafe(self):
        # A scanner outage must not make files trusted by default.
        attachment = self._store()
        storages["private"].delete(attachment.storage_key)

        attachment = services.scan_attachment(attachment)

        self.assertEqual(attachment.scan_status, ScanStatus.ERROR)
        self.assertFalse(attachment.is_servable)

    def test_scan_is_queued_on_commit_not_run_inline(self):
        with self.captureOnCommitCallbacks(execute=True):
            attachment = services.store(
                uploaded_file=factories.pdf_upload(),
                accepted_types=DOCUMENT_TYPES,
                uploaded_by=self.user,
            )
        attachment.refresh_from_db()
        self.assertEqual(attachment.scan_status, ScanStatus.CLEAN)


class PurgeTests(TestCase):
    def setUp(self):
        self.user = create_user()
        reset_scanner_cache()
        self.addCleanup(reset_scanner_cache)

    def test_soft_deleted_attachment_bytes_are_purged(self):
        attachment = services.store(
            uploaded_file=factories.pdf_upload(),
            accepted_types=DOCUMENT_TYPES,
            uploaded_by=self.user,
            scan=False,
        )
        key = attachment.storage_key
        services.soft_delete(attachment=attachment, actor=self.user)

        self.assertEqual(services.purge(), 1)
        self.assertFalse(storages["private"].exists(key))
        self.assertEqual(Attachment.objects.count(), 0)

    def test_shared_bytes_survive_while_another_row_points_at_them(self):
        # Content-addressed storage means two rows can share one object.
        kept = services.store(
            uploaded_file=factories.pdf_upload(),
            accepted_types=DOCUMENT_TYPES,
            uploaded_by=self.user,
            scan=False,
        )
        doomed = services.store(
            uploaded_file=factories.pdf_upload(),
            accepted_types=DOCUMENT_TYPES,
            uploaded_by=self.user,
            scan=False,
        )
        services.soft_delete(attachment=doomed, actor=self.user)

        services.purge()

        self.assertTrue(storages["private"].exists(kept.storage_key))
