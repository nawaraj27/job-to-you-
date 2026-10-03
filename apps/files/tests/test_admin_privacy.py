"""
Decision D-01: Django `is_staff` opens the admin site and nothing else.

A private file — a CV, a message attachment, a verification document — is read
only through the StaffRole-gated, audited staff screens. These tests pin the
admin half of that rule: the admin never serves bytes, never offers a link that
would, and cannot delete a file row behind the soft delete's back. The token
route half is `test_access.py` (`test_the_django_staff_flag_does_not_open_...`).
"""

from __future__ import annotations

from django.contrib import admin as django_admin
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_staff, create_user
from apps.files.constants import ScanStatus, Visibility
from apps.files.models import Attachment, AttachmentAccess


def _private_cv(owner) -> Attachment:
    return Attachment.objects.create(
        storage_key="ab/cd/private-cv.pdf",
        filename="private-cv.pdf",
        mime="application/pdf",
        size=10,
        sha256="e" * 64,
        visibility=Visibility.PRIVATE,
        scan_status=ScanStatus.CLEAN,
        uploaded_by=owner,
    )


class DjangoAdminFileTests(TestCase):
    def setUp(self):
        self.attachment = _private_cv(create_user())

    def test_is_staff_alone_opens_no_file_screen_in_the_admin(self):
        # `is_staff` without model permissions: the admin site answers, the
        # attachment screens do not.
        self.client.force_login(create_staff())
        for name, args in (
            ("admin:files_attachment_changelist", []),
            ("admin:files_attachment_change", [self.attachment.pk]),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)

    def test_a_superuser_sees_metadata_and_no_way_to_the_bytes(self):
        self.client.force_login(create_staff(is_superuser=True))

        page = self.client.get(reverse("admin:files_attachment_change", args=[self.attachment.pk]))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "private-cv.pdf")
        # No download route and no storage URL anywhere on the page.
        # (`/admin/files/...` is the admin's own URL for this model.)
        self.assertNotContains(page, 'href="/files/')
        self.assertNotContains(page, 'href="/media/')
        self.assertFalse(AttachmentAccess.objects.exists())

    def test_the_attachment_admin_cannot_change_or_delete(self):
        model_admin = django_admin.site._registry[Attachment]
        request = type("R", (), {"user": create_staff(is_superuser=True)})()
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, self.attachment))
        self.assertFalse(model_admin.has_delete_permission(request, self.attachment))

        self.client.force_login(request.user)
        response = self.client.post(
            reverse("admin:files_attachment_delete", args=[self.attachment.pk]), {"post": "yes"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Attachment.objects.filter(pk=self.attachment.pk).exists())

    def test_no_private_file_model_is_registered_with_a_file_field(self):
        # The admin renders a FileField as a link to its URL. None of the
        # registered models may carry one, or the admin becomes a download
        # route that skips the policy and the access log.
        from django.db.models import FileField

        for model in django_admin.site._registry:
            with self.subTest(model=model.__name__):
                self.assertFalse(
                    any(isinstance(field, FileField) for field in model._meta.get_fields()),
                    f"{model.__name__} exposes a FileField in the admin",
                )

    def test_verification_documents_and_messages_are_not_in_the_admin(self):
        from apps.companies.models import CompanyVerificationDocument
        from apps.messaging.models import Message

        self.assertNotIn(CompanyVerificationDocument, django_admin.site._registry)
        self.assertNotIn(Message, django_admin.site._registry)
