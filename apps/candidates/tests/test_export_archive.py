"""
FR-1.9 (Must): "Data export as JSON plus original files" (Loop Closure TAL-01).

The export was JSON only: the candidate's uploaded CVs, certificate files and
the attachments they sent were not in it, so the access right delivered a
description of their files rather than the files.
"""

from __future__ import annotations

import io
import json
import zipfile
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.candidates.models import ProfileCertification
from apps.candidates.services import erasure
from apps.candidates.tests.factories import create_profile
from apps.files.constants import DOCUMENT_TYPES, Visibility
from apps.files.models import AttachmentAccess
from apps.files.services import store
from apps.files.tests import factories as file_factories
from apps.resumes.models import ResumeUpload


def _file(user, *, clean=True, name="cv.pdf"):
    upload = file_factories.pdf_upload()
    upload.name = name
    attachment = store(
        uploaded_file=upload,
        accepted_types=DOCUMENT_TYPES,
        visibility=Visibility.PRIVATE,
        uploaded_by=user,
        scan=False,
    )
    attachment.scan_status = "clean" if clean else "pending"
    attachment.save(update_fields=["scan_status"])
    return attachment


class ExportArchiveTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.user = self.profile.user
        self.cv = _file(self.user, name="my-cv.pdf")
        ResumeUpload.objects.create(
            profile=self.profile,
            attachment=self.cv,
            original_filename="my-cv.pdf",
            mime=self.cv.mime,
            size=self.cv.size,
            sha256=self.cv.sha256,
        )
        self.certificate = _file(self.user, name="certificate.pdf")
        ProfileCertification.objects.create(
            profile=self.profile, name="AWS Practitioner", file=self.certificate
        )
        self.client.force_login(self.user)

    def _archive(self):
        response = self.client.get(reverse("settings:export_archive"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("careerai-export.zip", response["Content-Disposition"])
        return zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))

    def test_the_zip_holds_the_json_and_the_original_files(self):
        archive = self._archive()
        names = set(archive.namelist())

        self.assertIn("careerai-export.json", names)
        self.assertIn("files/cvs/my-cv.pdf", names)
        self.assertIn("files/certificates/certificate.pdf", names)
        data = json.loads(archive.read("careerai-export.json"))
        self.assertEqual(len(data["original_files"]["included"]), 2)
        self.assertIn("profile", data)
        self.assertTrue(archive.read("files/cvs/my-cv.pdf").startswith(b"%PDF"))

    def test_each_packed_file_is_logged_as_a_read(self):
        self._archive()

        purposes = set(AttachmentAccess.objects.values_list("attachment_id", "purpose"))
        self.assertEqual(
            purposes, {(self.cv.pk, "data_export"), (self.certificate.pk, "data_export")}
        )

    def test_unscanned_files_and_other_peoples_files_are_left_out(self):
        pending = _file(self.user, clean=False, name="pending.pdf")
        ProfileCertification.objects.create(profile=self.profile, name="Pending", file=pending)
        someone_else = create_profile()
        theirs = _file(someone_else.user, name="theirs.pdf")
        ResumeUpload.objects.create(
            profile=someone_else,
            attachment=theirs,
            original_filename="theirs.pdf",
            mime=theirs.mime,
            size=theirs.size,
            sha256=theirs.sha256,
        )

        names = set(self._archive().namelist())

        self.assertNotIn("files/certificates/pending.pdf", names)
        self.assertNotIn("files/cvs/theirs.pdf", names)

    def test_files_past_the_size_limit_are_listed_not_silently_dropped(self):
        with mock.patch.object(erasure, "ARCHIVE_FILE_BYTES_LIMIT", self.cv.size):
            archive = self._archive()

        data = json.loads(archive.read("careerai-export.json"))
        self.assertEqual(len(data["original_files"]["included"]), 1)
        self.assertEqual(data["original_files"]["left_out"][0]["reason"], "size limit")

    def test_the_data_screen_offers_the_zip(self):
        response = self.client.get(reverse("settings:data"))
        self.assertContains(response, reverse("settings:export_archive"))

    def test_signed_out_visitors_get_nothing(self):
        self.client.logout()
        response = self.client.get(reverse("settings:export_archive"))
        self.assertNotEqual(response.status_code, 200)

    def test_another_account_gets_none_of_my_files(self):
        self.client.force_login(create_user())
        response = self.client.get(reverse("settings:export_archive"))

        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))
        self.assertEqual(archive.namelist(), ["careerai-export.json"])
        data = json.loads(archive.read("careerai-export.json"))
        self.assertEqual(data["original_files"]["included"], [])
