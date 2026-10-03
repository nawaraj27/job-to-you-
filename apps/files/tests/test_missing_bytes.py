"""
A file row whose bytes are missing from storage answers 404, never 500.

Found by the link sweep on 2026-09-18: a verification document whose stored
object was absent crashed the staff document route with FileNotFoundError.
The token route shares the same streaming helper.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import StaffRole
from apps.accounts.tests.factories import create_staff, create_user
from apps.companies.models import CompanyVerificationDocument
from apps.companies.services import company as company_service
from apps.files import signing
from apps.files.constants import ScanStatus, Visibility
from apps.files.models import Attachment


def _ghost(**fields) -> Attachment:
    return Attachment.objects.create(
        storage_key="ab/cd/not-on-disk.pdf",
        filename="gone.pdf",
        mime="application/pdf",
        size=10,
        sha256="c" * 64,
        scan_status=ScanStatus.CLEAN,
        **fields,
    )


class MissingBytesTests(TestCase):
    def test_the_staff_document_route_answers_404(self):
        owner = create_user()
        company = company_service.create_company(actor=owner, name="Ghost Files")
        document = CompanyVerificationDocument.objects.create(
            verification=company.verification,
            kind="registration",
            attachment=_ghost(visibility=Visibility.COMPANY, owner_company=company),
            uploaded_by=owner,
        )
        reviewer = create_staff()
        StaffRole.objects.create(user=reviewer, level="moderator")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("staff:verification_document", args=[document.pk]))

        self.assertEqual(response.status_code, 404)

    def test_the_token_route_answers_404(self):
        owner = create_user()
        attachment = _ghost(visibility=Visibility.PRIVATE, uploaded_by=owner)
        self.client.force_login(owner)
        token = signing.issue(attachment, purpose="download")

        response = self.client.get(
            reverse("files:access", args=[attachment.public_id]), {"t": token}
        )

        self.assertEqual(response.status_code, 404)
