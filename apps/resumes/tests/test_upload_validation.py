"""
What a CV upload may be, decided before anything is stored.

Every refusal is asserted from the outside — through the upload screen — and
with the three things a refusal must leave behind: the standard sentence, a
reason the person can act on, and **nothing else**: no `ResumeUpload`, no
`Attachment`, no file in storage, no parse job.

The `.doc` fixtures in `fixtures/` were produced by Microsoft Word (Save As
"Word 97-2003 Document") from python-docx sources, because a hand-built
compound file would test this reader against itself. `cv-password.*` are
encrypted with a throwaway password that appears nowhere else.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.candidates.tests.factories import create_profile
from apps.files.constants import DOC, DOCX, PDF
from apps.files.models import Attachment
from apps.resumes.models import ParseStatus, ResumeUpload
from apps.resumes.parsing.text import extract_text
from apps.resumes.services import upload as upload_service
from apps.resumes.services import validation
from apps.resumes.tests.factories import SAMPLE_CV, docx_bytes, pdf_bytes

FIXTURES = Path(__file__).parent / "fixtures"
HEADLINE = "Upload your CV or resume in PDF, DOC, or DOCX format."


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def zip_bytes(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


DOCX_TYPES = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.{kind}+xml"/></Types>'
)

INVOICE = "INVOICE 2026-0042\nBill to: Himal Traders\nAmount due: NPR 12,000\nPay by 30 October\nThank you for your business"


class UploadFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)

    def setUp(self):
        cache.clear()
        self.profile = create_profile()
        self.client.force_login(self.profile.user)

    def post(self, name: str, data: bytes, content_type: str = "application/octet-stream"):
        return self.client.post(
            reverse("resume:upload_receive"),
            {"file": SimpleUploadedFile(name, data, content_type=content_type)},
            HTTP_HX_REQUEST="true",
        )

    def assert_refused(
        self, name: str, data: bytes, reason: str, content_type: str = "application/pdf"
    ):
        files_before = Attachment.objects.count()
        with (
            mock.patch("apps.resumes.services.upload._enqueue") as parse_job,
            mock.patch("apps.files.services._enqueue_scan") as scan_job,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.post(name, data, content_type)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, HEADLINE)
        self.assertContains(response, reason)
        self.assertFalse(ResumeUpload.objects.filter(profile=self.profile).exists())
        self.assertEqual(Attachment.objects.count(), files_before)
        parse_job.assert_not_called()
        scan_job.assert_not_called()
        return response

    def assert_accepted(
        self, name: str, data: bytes, content_type: str = "application/pdf"
    ) -> ResumeUpload:
        response = self.post(name, data, content_type)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, HEADLINE)
        return ResumeUpload.objects.get(profile=self.profile)


class AcceptedFormatTests(UploadFixture):
    def test_a_pdf_cv(self):
        upload = self.assert_accepted("cv.pdf", pdf_bytes(SAMPLE_CV))
        self.assertEqual(upload.mime, PDF)
        self.assertEqual(upload.pages, 1)

    def test_a_docx_cv(self):
        upload = self.assert_accepted("cv.docx", docx_bytes(), DOCX)
        self.assertEqual(upload.mime, DOCX)

    def test_a_word_97_doc_cv(self):
        upload = self.assert_accepted("cv.doc", fixture("cv.doc"), "application/msword")
        self.assertEqual(upload.mime, DOC)
        self.assertEqual(upload.parse_status, ParseStatus.PENDING)

    def test_extension_case_does_not_matter(self):
        self.assert_accepted("MY CV.PDF", pdf_bytes(SAMPLE_CV))

    def test_the_minimal_cv_still_passes(self):
        # A name, one heading and a line under it is a thin CV, not a non-CV.
        self.assert_accepted("cv.pdf", pdf_bytes("Priya Sharma\nSkills\nPython, Django"))

    def test_the_browser_is_told_which_types_to_offer(self):
        page = self.client.get(reverse("resume:upload"))
        self.assertContains(page, 'accept=".pdf,.doc,.docx')
        self.assertContains(page, "PDF, DOC or DOCX")


class DocReaderTests(TestCase):
    def test_the_text_of_a_doc_is_read_in_order(self):
        text = extract_text(fixture("cv.doc"), mime=DOC)
        self.assertTrue(text.startswith("Priya Sharma\nBackend Engineer"))
        self.assertIn("Backend Engineer, Nexa Payments", text)
        self.assertLess(text.index("Experience"), text.index("Education"))
        self.assertLess(text.index("Education"), text.index("Skills"))

    def test_table_cells_are_kept_apart(self):
        text = extract_text(fixture("cv-table.doc"), mime=DOC)
        self.assertIn("Staff Nurse | Bir Hospital | 2019 - Present", text)

    def test_a_doc_parses_like_the_same_cv_in_docx(self):
        from apps.resumes.parsing import parse

        from_doc = parse(extract_text(fixture("cv.doc"), mime=DOC), mime=DOC)
        from_docx = parse(extract_text(docx_bytes(), mime=DOCX), mime=DOCX)
        self.assertEqual(from_doc.name, from_docx.name)
        self.assertEqual(from_doc.emails, from_docx.emails)
        self.assertEqual(from_doc.skill_terms, from_docx.skill_terms)
        self.assertEqual(len(from_doc.experience), len(from_docx.experience))

    def test_a_damaged_compound_file_fails_cleanly(self):
        from apps.resumes.parsing.base import ExtractionFailed

        data = bytearray(fixture("cv.doc"))
        data[600:5000] = b"\xff" * 4400
        with self.assertRaises(ExtractionFailed):
            extract_text(bytes(data), mime=DOC)


class RefusedFormatTests(UploadFixture):
    def test_an_empty_file(self):
        self.assert_refused("cv.pdf", b"", "That file is empty.")

    @override_settings(FILES_MAX_UPLOAD_BYTES=1024)
    def test_an_oversized_file(self):
        self.assert_refused("cv.pdf", pdf_bytes(SAMPLE_CV), "larger than the 0 MB limit")

    def test_an_image(self):
        self.assert_refused(
            "cv.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "That file is an image", "image/png"
        )

    def test_a_jpeg_photo_of_a_cv(self):
        self.assert_refused(
            "cv.jpg", b"\xff\xd8\xff\xe0" + b"0" * 64, "That file is an image", "image/jpeg"
        )

    def test_an_image_renamed_pdf(self):
        self.assert_refused(
            "cv.pdf", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "named .pdf but its contents are not"
        )

    def test_a_zip(self):
        self.assert_refused(
            "cv.zip", zip_bytes({"cv.pdf": "x"}), "compressed archive", "application/zip"
        )

    def test_a_zip_renamed_docx(self):
        self.assert_refused(
            "cv.docx", zip_bytes({"a.txt": "hello"}), "compressed archive (ZIP)", DOCX
        )

    def test_an_executable_renamed_pdf(self):
        self.assert_refused("cv.pdf", b"MZ\x90\x00" + b"\x00" * 200, "That file is a program")

    def test_an_executable(self):
        self.assert_refused("setup.exe", b"MZ\x90\x00" + b"\x00" * 200, "That file is a program")

    def test_html(self):
        self.assert_refused(
            "cv.html",
            b"<!doctype html><html><body>CV</body></html>",
            "web page (HTML)",
            "text/html",
        )

    def test_html_renamed_pdf(self):
        self.assert_refused(
            "cv.pdf", b"<html><body>Priya Sharma CV</body></html>", "web page (HTML)"
        )

    def test_xml(self):
        self.assert_refused("cv.xml", b"<?xml version='1.0'?><cv/>", "XML data file", "text/xml")

    def test_csv(self):
        self.assert_refused(
            "cv.csv", b"name,email\npriya,p@example.com\n", "spreadsheet (CSV)", "text/csv"
        )

    def test_csv_renamed_pdf(self):
        self.assert_refused(
            "cv.pdf", b"name,email\npriya,p@example.com\nram,r@example.com\n", "spreadsheet (CSV)"
        )

    def test_excel(self):
        data = zip_bytes({"[Content_Types].xml": "<Types/>", "xl/workbook.xml": "<w/>"})
        self.assert_refused("cv.xlsx", data, "spreadsheet, not a CV")

    def test_excel_renamed_docx(self):
        data = zip_bytes({"[Content_Types].xml": "<Types/>", "xl/workbook.xml": "<w/>"})
        self.assert_refused("cv.docx", data, "spreadsheet, not a CV")

    def test_powerpoint(self):
        data = zip_bytes({"[Content_Types].xml": "<Types/>", "ppt/presentation.xml": "<p/>"})
        self.assert_refused("cv.pptx", data, "presentation, not a CV")

    def test_plain_text(self):
        self.assert_refused("cv.txt", SAMPLE_CV.encode(), "plain text", "text/plain")

    def test_rtf(self):
        self.assert_refused("cv.rtf", b"{\\rtf1\\ansi Priya}", "Rich Text (RTF)")

    def test_opendocument(self):
        self.assert_refused(
            "cv.odt",
            zip_bytes({"mimetype": "application/vnd.oasis.opendocument.text"}),
            "OpenDocument",
        )

    def test_a_file_with_no_extension(self):
        self.assert_refused("cv", pdf_bytes(SAMPLE_CV), "has no extension")

    def test_an_unknown_extension(self):
        self.assert_refused("cv.pages2", pdf_bytes(SAMPLE_CV), "Files ending in .pages2")

    def test_a_docx_renamed_pdf(self):
        self.assert_refused("cv.pdf", docx_bytes(), "actually a Word (.docx) file")

    def test_a_pdf_renamed_docx(self):
        self.assert_refused("cv.docx", pdf_bytes(SAMPLE_CV), "actually a PDF file", DOCX)

    def test_a_doc_renamed_docx(self):
        self.assert_refused(
            "cv.docx", fixture("cv.doc"), "actually a Word 97-2003 (.doc) file", DOCX
        )


class RefusedStructureTests(UploadFixture):
    def test_a_corrupt_pdf(self):
        self.assert_refused("cv.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 50, "That PDF is damaged")

    def test_a_password_protected_pdf(self):
        self.assert_refused("cv.pdf", fixture("cv-password.pdf"), "That PDF is password-protected")

    def test_a_password_protected_doc(self):
        self.assert_refused("cv.doc", fixture("cv-password.doc"), "password-protected", DOC)

    def test_a_garbage_doc(self):
        self.assert_refused(
            "cv.doc",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504,
            "isn&#x27;t a PDF or Word document",
            DOC,
        )

    def test_a_macro_enabled_document_renamed_docx(self):
        data = zip_bytes(
            {
                "[Content_Types].xml": DOCX_TYPES.format(kind="document.macroEnabled.main"),
                "word/document.xml": "<w:document/>",
                "word/vbaProject.bin": b"\x00",
            }
        )
        self.assert_refused("cv.docx", data, "macro-enabled Word document", DOCX)

    def test_a_template_renamed_docx(self):
        data = zip_bytes(
            {
                "[Content_Types].xml": DOCX_TYPES.format(kind="template.main"),
                "word/document.xml": "<w:document/>",
            }
        )
        self.assert_refused("cv.docx", data, "Word template", DOCX)

    def test_a_docx_whose_document_is_not_xml(self):
        data = zip_bytes(
            {
                "[Content_Types].xml": DOCX_TYPES.format(kind="document.main"),
                "word/document.xml": "not xml at all",
            }
        )
        self.assert_refused("cv.docx", data, "That Word file is damaged", DOCX)

    def test_a_docx_with_a_doctype_is_refused(self):
        data = zip_bytes(
            {
                "[Content_Types].xml": DOCX_TYPES.format(kind="document.main"),
                "word/document.xml": '<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document>&a;</w:document>',
            }
        )
        self.assert_refused("cv.docx", data, "can&#x27;t be read safely", DOCX)

    def test_a_zip_bomb_is_refused_without_unpacking_it(self):
        bomb = zip_bytes(
            {
                "[Content_Types].xml": DOCX_TYPES.format(kind="document.main"),
                "word/document.xml": "<w:document/>",
                "word/media/zeros.bin": b"\x00" * (validation.MAX_UNPACKED_BYTES + 1),
            }
        )
        self.assertLess(len(bomb), 1024 * 1024)
        self.assert_refused("cv.docx", bomb, "unpacks to an unreasonable size", DOCX)


class RefusedContentTests(UploadFixture):
    def test_an_invoice_pdf_is_not_a_cv(self):
        self.assert_refused("invoice.pdf", pdf_bytes(INVOICE), "doesn&#x27;t look like a CV")

    def test_an_invoice_doc_is_not_a_cv(self):
        self.assert_refused(
            "invoice.doc", fixture("invoice.doc"), "doesn&#x27;t look like a CV", DOC
        )

    def test_an_invoice_docx_is_not_a_cv(self):
        self.assert_refused(
            "invoice.docx", docx_bytes(INVOICE), "doesn&#x27;t look like a CV", DOCX
        )

    def test_a_pdf_with_no_text_layer_reads_as_a_scan(self):
        self.assert_refused("cv.pdf", pdf_bytes(""), "looks like a scan or a photo")

    def test_a_nearly_empty_file(self):
        self.assert_refused("cv.docx", docx_bytes("Priya"), "almost nothing in it", DOCX)

    def test_unknown_headings_with_dated_roles_and_contact_still_pass(self):
        text = (
            "Priya Sharma\npriya@example.com\n\nWhat I have done\n"
            "Backend Engineer at Nexa 2021 - Present\nDeveloper at Himal 2019 - 2021\n"
        )
        self.assert_accepted("cv.pdf", pdf_bytes(text))


class GuestUploadTests(TestCase):
    def setUp(self):
        cache.clear()

    def post(self, name, data):
        return self.client.post(
            reverse("resume:guest_upload"), {"file": SimpleUploadedFile(name, data)}
        )

    def test_a_guest_image_is_refused_with_the_reason(self):
        response = self.post("cv.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, HEADLINE, status_code=400)
        self.assertContains(response, "That file is an image", status_code=400)
        self.assertFalse(ResumeUpload.objects.exists())
        self.assertFalse(Attachment.objects.exists())

    def test_a_guest_doc_is_accepted(self):
        response = self.post("cv.doc", fixture("cv.doc"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ResumeUpload.objects.get().mime, DOC)

    def test_guest_content_is_not_read_before_claim(self):
        # ADR-A32: structure is checked, content is not. An invoice in a
        # well-formed PDF is held (blocked) and only judged once claimed.
        with mock.patch("apps.resumes.services.validation._check_is_a_cv") as content_gate:
            response = self.post("invoice.pdf", pdf_bytes(INVOICE))
        self.assertEqual(response.status_code, 200)
        content_gate.assert_not_called()
        self.assertEqual(ResumeUpload.objects.get().parse_status, ParseStatus.BLOCKED)

    def test_guest_structure_is_still_checked(self):
        response = self.post("cv.pdf", fixture("cv-password.pdf"))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "password-protected", status_code=400)


class NothingLeftBehindTests(UploadFixture):
    def test_a_refused_file_is_not_written_to_storage(self):
        private = storages["private"]
        with mock.patch.object(type(private), "save", autospec=True) as save:
            self.post("cv.pdf", pdf_bytes(INVOICE))
        save.assert_not_called()

    def test_the_same_bytes_under_a_wrong_name_are_still_refused(self):
        self.assert_accepted("cv.docx", docx_bytes(), DOCX)
        response = self.post("cv.pdf", docx_bytes(), "application/pdf")
        self.assertContains(response, "actually a Word (.docx) file")
        self.assertEqual(ResumeUpload.objects.filter(profile=self.profile).count(), 1)


class ParseReuseTests(UploadFixture):
    def test_the_parse_reuses_the_text_validation_read(self):
        from apps.resumes.services.parse import parse_upload

        upload = self.assert_accepted("cv.pdf", pdf_bytes(SAMPLE_CV))
        upload.attachment.scan_status = "clean"
        upload.attachment.save(update_fields=["scan_status"])
        with (
            mock.patch("apps.resumes.services.parse.extract_text") as extract,
            mock.patch("apps.resumes.services.parse.page_count") as pages,
        ):
            record = parse_upload(upload=upload)
        extract.assert_not_called()
        pages.assert_not_called()
        self.assertIn("Nexa Payments", record.raw_text)

    def test_without_the_handoff_the_parse_reads_the_file(self):
        from apps.resumes.services.parse import parse_upload

        upload = self.assert_accepted("cv.docx", docx_bytes(), DOCX)
        upload.attachment.scan_status = "clean"
        upload.attachment.save(update_fields=["scan_status"])
        cache.delete(upload_service.handoff_key(upload.sha256))
        record = parse_upload(upload=upload)
        self.assertIn("Nexa Payments", record.raw_text)
