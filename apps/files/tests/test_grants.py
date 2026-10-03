"""
Attachment grants (ADR-A52).

A private file is readable by its uploader alone, which is right for a CV and
wrong for a message attachment. These tests hold both halves of that: a
stranger still cannot read one, and the person it was actually sent to can.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.common.errors import NotFound
from apps.files import policies, services
from apps.files.constants import ScanStatus, Visibility
from apps.files.models import Attachment, AttachmentGrant


def _attachment(uploader, **overrides) -> Attachment:
    fields = {
        "storage_key": "ab/cd/abcd.pdf",
        "filename": "cv.pdf",
        "mime": "application/pdf",
        "size": 1024,
        "sha256": "a" * 64,
        "visibility": Visibility.PRIVATE,
        "scan_status": ScanStatus.CLEAN,
        "uploaded_by": uploader,
    }
    fields.update(overrides)
    return Attachment.objects.create(**fields)


class GrantTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.other = create_user(email="other@example.test")
        self.attachment = _attachment(self.owner)

    def test_the_uploader_can_read_their_own_file(self):
        self.assertTrue(policies.can_access(self.owner, self.attachment))

    def test_a_stranger_cannot(self):
        self.assertFalse(policies.can_access(self.other, self.attachment))
        with self.assertRaises(NotFound):
            policies.require_can_access(self.other, self.attachment)

    def test_a_grant_lets_the_named_person_read_it(self):
        services.share_with(attachment=self.attachment, user=self.other, reason="message")
        self.assertTrue(policies.can_access(self.other, self.attachment))

    def test_a_grant_reaches_nobody_else(self):
        third = create_user(email="third@example.test")
        services.share_with(attachment=self.attachment, user=self.other, reason="message")
        self.assertFalse(policies.can_access(third, self.attachment))

    def test_sharing_twice_for_one_reason_makes_one_row(self):
        for _ in range(2):
            services.share_with(attachment=self.attachment, user=self.other, reason="message")
        self.assertEqual(AttachmentGrant.objects.count(), 1)

    def test_revoking_takes_the_access_back(self):
        services.share_with(attachment=self.attachment, user=self.other, reason="message")
        services.revoke_share(attachment=self.attachment, user=self.other, reason="message")
        self.assertFalse(policies.can_access(self.other, self.attachment))

    def test_revoking_one_reason_leaves_another_standing(self):
        services.share_with(attachment=self.attachment, user=self.other, reason="message")
        services.share_with(attachment=self.attachment, user=self.other, reason="application")
        services.revoke_share(attachment=self.attachment, user=self.other, reason="message")

        self.assertTrue(policies.can_access(self.other, self.attachment))

    def test_an_expired_grant_does_not_grant(self):
        services.share_with(
            attachment=self.attachment,
            user=self.other,
            reason="message",
            expires_at=timezone.now() - dt.timedelta(hours=1),
        )
        self.assertFalse(policies.can_access(self.other, self.attachment))

    def test_a_grant_cannot_resurrect_a_deleted_file(self):
        services.share_with(attachment=self.attachment, user=self.other, reason="message")
        self.attachment.deleted_at = timezone.now()
        self.attachment.save(update_fields=["deleted_at"])

        self.assertFalse(policies.can_access(self.other, self.attachment))

    def test_a_grant_cannot_bypass_the_scanner(self):
        # An infected file is not servable to anybody, grant or no grant.
        infected = _attachment(
            self.owner,
            storage_key="ef/gh/efgh.pdf",
            sha256="b" * 64,
            scan_status=ScanStatus.INFECTED,
        )
        services.share_with(attachment=infected, user=self.other, reason="message")

        self.assertFalse(policies.can_access(self.other, infected))
