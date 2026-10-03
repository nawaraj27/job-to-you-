"""
Signed, single-use, policy-checked, logged file access (SRS §14.2).

This is the security boundary the SRS calls out as the most common
catastrophic failure in ATS products: a private resume reachable by URL.
"""

from __future__ import annotations

import time
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.tests.factories import create_staff, create_user
from apps.common.errors import NotAllowed
from apps.files import services, signing
from apps.files.constants import DOCUMENT_TYPES, ScanStatus, Visibility
from apps.files.models import AttachmentAccess
from apps.files.scanning import reset_scanner_cache
from apps.files.tests import factories


class SigningTests(TestCase):
    def setUp(self):
        self.attachment = _clean_attachment()

    def test_a_valid_token_verifies_once(self):
        token = signing.issue(self.attachment, purpose="download")

        payload = signing.verify(token, expected_public_id=str(self.attachment.public_id))

        self.assertEqual(payload["pur"], "download")

    def test_a_token_cannot_be_replayed(self):
        token = signing.issue(self.attachment)
        signing.verify(token, expected_public_id=str(self.attachment.public_id))

        with self.assertRaises(NotAllowed) as caught:
            signing.verify(token, expected_public_id=str(self.attachment.public_id))
        self.assertIn("already been used", str(caught.exception))

    def test_a_tampered_token_is_refused(self):
        token = signing.issue(self.attachment)
        with self.assertRaises(NotAllowed):
            signing.verify(token[:-2] + "xy", expected_public_id=str(self.attachment.public_id))

    def test_a_token_for_another_file_is_refused(self):
        other = _clean_attachment()
        token = signing.issue(other)

        with self.assertRaises(NotAllowed):
            signing.verify(token, expected_public_id=str(self.attachment.public_id))

    def test_an_expired_token_is_refused(self):
        token = signing.issue(self.attachment)
        with (
            mock.patch("time.time", return_value=time.time() + 10_000),
            self.assertRaises(NotAllowed) as caught,
        ):
            signing.verify(token, expected_public_id=str(self.attachment.public_id))
        self.assertIn("expired", str(caught.exception))


class FileAccessViewTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.attachment = _clean_attachment(uploaded_by=self.owner)

    def _url(self, attachment=None, token=None):
        attachment = attachment or self.attachment
        url = reverse("files:access", args=[attachment.public_id])
        token = signing.issue(attachment) if token is None else token
        return f"{url}?t={token}" if token else url

    def _get(self, url):
        """GET a file URL and release the streamed file handle.

        A served file is a FileResponse holding an open handle. The test client
        never closes it, and because storage keys are content-addressed, two
        tests using the same bytes share one path — so a handle leaked by one
        test makes a later test's delete fail on Windows, and the suite becomes
        order-dependent.

        The handle is closed directly rather than through `response.close()`:
        that fires `request_finished`, which closes the database connection out
        from under the rest of the test.
        """
        response = self.client.get(url)
        handle = getattr(response, "file_to_stream", None)
        if handle is not None and not handle.closed:
            handle.close()
        return response

    def test_owner_can_download_with_a_valid_token(self):
        self.client.force_login(self.owner)

        response = self._get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_download_is_never_inline(self):
        # A user-supplied PDF rendered inline is script execution in this origin.
        self.client.force_login(self.owner)
        response = self._get(self._url())
        self.assertNotIn("inline", response["Content-Disposition"])

    def test_no_token_is_refused(self):
        self.client.force_login(self.owner)
        response = self._get(reverse("files:access", args=[self.attachment.public_id]))
        self.assertEqual(response.status_code, 404)

    def test_anonymous_visitor_is_refused(self):
        response = self._get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_another_user_holding_a_valid_token_is_still_refused(self):
        # The token proves a link was issued; the policy decides who may read.
        stranger = create_user()
        self.client.force_login(stranger)

        response = self._get(self._url())

        self.assertEqual(response.status_code, 404)

    def test_missing_file_and_forbidden_file_are_indistinguishable(self):
        stranger = create_user()
        self.client.force_login(stranger)
        forbidden = self._get(self._url())

        import uuid

        missing = self.client.get(f"{reverse('files:access', args=[uuid.uuid4()])}?t=whatever")

        self.assertEqual(forbidden.status_code, missing.status_code)

    def test_unscanned_file_cannot_be_downloaded(self):
        pending = _clean_attachment(uploaded_by=self.owner)
        pending.scan_status = ScanStatus.PENDING
        pending.save(update_fields=["scan_status"])
        self.client.force_login(self.owner)

        response = self._get(self._url(pending))

        self.assertEqual(response.status_code, 404)

    def test_the_django_staff_flag_does_not_open_someone_elses_file(self):
        """`is_staff` used to read every private file with a token in hand.

        `apps/accounts/staff.py`: the flag governs the admin site and nothing
        else. A superuser is refused the same way; staff read files only
        through StaffRole-gated, audited screens.
        """
        for actor in (create_staff(), create_staff(is_superuser=True)):
            with self.subTest(superuser=actor.is_superuser):
                self.client.force_login(actor)
                response = self._get(self._url())
                self.assertEqual(response.status_code, 404)
        self.assertEqual(AttachmentAccess.objects.count(), 0)

    def test_the_django_staff_flag_does_not_list_other_peoples_files(self):
        from apps.files import selectors

        self.assertFalse(selectors.for_actor(create_staff()).filter(pk=self.attachment.pk).exists())
        self.assertTrue(selectors.for_actor(self.owner).filter(pk=self.attachment.pk).exists())

    def test_every_read_is_logged_before_the_bytes_are_served(self):
        self.client.force_login(self.owner)

        self._get(self._url())

        access = AttachmentAccess.objects.get()
        self.assertEqual(access.attachment, self.attachment)
        self.assertEqual(access.actor, self.owner)

    def test_a_refused_read_is_not_logged_as_an_access(self):
        create_user()
        self.client.force_login(create_user())
        self._get(self._url())
        self.assertEqual(AttachmentAccess.objects.count(), 0)

    def test_a_token_works_only_once_through_the_view(self):
        self.client.force_login(self.owner)
        url = self._url()

        self.assertEqual(self._get(url).status_code, 200)
        self.assertEqual(self._get(url).status_code, 403)

    def test_public_attachment_needs_no_token(self):
        public = _clean_attachment(visibility=Visibility.PUBLIC)
        response = self._get(reverse("files:access", args=[public.public_id]))
        self.assertEqual(response.status_code, 200)

    def test_company_visibility_has_no_reader_before_memberships_exist(self):
        # Correct for the current data model: company access is granted through
        # CompanyMembership, which arrives in P1.
        company_file = _clean_attachment(uploaded_by=self.owner, visibility=Visibility.COMPANY)
        self.client.force_login(self.owner)

        response = self._get(self._url(company_file))

        self.assertEqual(response.status_code, 404)


class PrivateStorageIsolationTests(TestCase):
    def test_private_storage_exposes_no_url(self):
        # If it ever gains one, private files become reachable without a policy
        # check and without an access log.
        from django.core.files.storage import storages

        with self.assertRaises(ValueError):
            storages["private"].url("ab/cd/abcd.pdf")

    @override_settings(DEBUG=False)
    def test_deploy_check_rejects_a_permissive_scanner(self):
        from apps.files.checks import check_scanner_is_not_permissive

        with self.settings(FILES_SCANNER="apps.files.scanning.AllowAllScanner"):
            errors = check_scanner_is_not_permissive(None)

        self.assertEqual([error.id for error in errors], ["files.E002"])

    def test_deploy_check_passes_with_a_real_scanner(self):
        from apps.files.checks import check_scanner_is_not_permissive

        with self.settings(DEBUG=False, FILES_SCANNER="apps.files.scanning.ClamAVScanner"):
            self.assertEqual(check_scanner_is_not_permissive(None), [])

    def test_private_storage_must_not_advertise_a_base_url(self):
        from apps.files.checks import check_private_storage_has_no_public_url

        broken = {
            "private": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": "var/test-private", "base_url": "/private/"},
            }
        }
        with self.settings(STORAGES=broken):
            errors = check_private_storage_has_no_public_url(None)

        self.assertEqual([error.id for error in errors], ["files.E003"])


def _clean_attachment(**overrides):
    reset_scanner_cache()
    attachment = services.store(
        uploaded_file=factories.pdf_upload(),
        accepted_types=DOCUMENT_TYPES,
        uploaded_by=overrides.pop("uploaded_by", None),
        visibility=overrides.pop("visibility", Visibility.PRIVATE),
        scan=False,
    )
    return services.scan_attachment(attachment)
