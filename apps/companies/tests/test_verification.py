"""
Company verification and suspension.

Verification is a state machine because the middle states are the product: an
employer who has submitted and is waiting needs to see that, and a system that
only knows "verified / not" has nothing to show them on the day they look
hardest.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_staff, create_user
from apps.audit.models import AuditLog
from apps.common.errors import InvalidState
from apps.companies.models import CompanyState, CompanyVerificationDocument, VerificationState
from apps.companies.services import company as company_service
from apps.files.constants import DOCUMENT_TYPES, Visibility
from apps.files.models import Attachment
from apps.files.scanning import reset_scanner_cache
from apps.files.services import store
from apps.files.tests import factories as file_factories
from apps.notifications.models import OutboxEvent


class VerificationStateMachineTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.staff = create_staff()
        self.company = company_service.create_company(actor=self.owner, name="Meridian Health")
        self._attach_document()

    def _attach_document(self) -> None:
        CompanyVerificationDocument.objects.create(
            verification=self.company.verification,
            kind="registration",
            attachment=Attachment.objects.create(
                storage_key="ab/cd/reg.pdf",
                filename="registration.pdf",
                mime="application/pdf",
                size=10,
                sha256="f" * 64,
                visibility=Visibility.COMPANY,
                owner_company=self.company,
            ),
            uploaded_by=self.owner,
        )

    def _decide(self, **kwargs):
        """A reviewer claims, then decides — the only order the service accepts (§19.5)."""
        company_service.claim_verification(actor=self.staff, company=self.company)
        return company_service.review_verification(actor=self.staff, company=self.company, **kwargs)

    def test_submitting_moves_into_review(self):
        verification = company_service.submit_verification(actor=self.owner, company=self.company)

        self.assertEqual(verification.state, VerificationState.PENDING)
        self.assertIsNotNone(verification.submitted_at)

    def test_the_company_row_mirrors_the_verification_state(self):
        # Both are read on hot paths — the badge on every listing, the banner in
        # the portal — and a mirror that can drift is worse than a join.
        company_service.submit_verification(actor=self.owner, company=self.company)
        self.company.refresh_from_db()

        self.assertEqual(self.company.verification_state, VerificationState.PENDING)

    def test_approving_marks_the_company_verified(self):
        company_service.submit_verification(actor=self.owner, company=self.company)

        self._decide(decision=VerificationState.VERIFIED)
        self.company.refresh_from_db()

        self.assertTrue(self.company.is_verified)
        self.assertIsNotNone(self.company.verified_at)

    def test_approval_emits_a_dedicated_event_as_well_as_the_state_change(self):
        # Subscribers that care only about success — badge, notification, search
        # boost — should not have to parse a state diff to find it.
        company_service.submit_verification(actor=self.owner, company=self.company)
        self._decide(decision=VerificationState.VERIFIED)

        self.assertTrue(OutboxEvent.objects.filter(type="company.verified").exists())
        self.assertTrue(
            OutboxEvent.objects.filter(type="company.verification_state_changed").exists()
        )

    def test_requesting_more_information_needs_a_reason(self):
        company_service.submit_verification(actor=self.owner, company=self.company)

        with self.assertRaises(InvalidState):
            self._decide(decision=VerificationState.MORE_INFO)

    def test_a_rejection_reason_reaches_the_company(self):
        company_service.submit_verification(actor=self.owner, company=self.company)

        verification = self._decide(
            decision=VerificationState.REJECTED,
            reason="The registration certificate is unreadable.",
        )

        self.assertIn("unreadable", verification.reason)

    def test_more_info_can_be_resubmitted(self):
        company_service.submit_verification(actor=self.owner, company=self.company)
        self._decide(
            decision=VerificationState.MORE_INFO,
            reason="Send the PAN certificate too.",
        )

        verification = company_service.submit_verification(actor=self.owner, company=self.company)

        self.assertEqual(verification.state, VerificationState.PENDING)

    def test_an_illegal_transition_is_refused(self):
        # not_submitted -> verified would let a company verify itself.
        with self.assertRaises(InvalidState):
            self._decide(decision=VerificationState.VERIFIED)

    def test_every_transition_is_audited(self):
        company_service.submit_verification(actor=self.owner, company=self.company)

        self.assertTrue(AuditLog.objects.filter(verb="company.verification_state_changed").exists())


class SuspensionTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.staff = create_staff()
        self.company = company_service.create_company(actor=self.owner, name="Orbit Retail")

    def test_suspension_records_a_reason_and_emits_an_event(self):
        company_service.suspend_company(
            actor=self.staff, company=self.company, reason="Fraudulent postings."
        )
        self.company.refresh_from_db()

        self.assertEqual(self.company.state, CompanyState.SUSPENDED)
        self.assertTrue(OutboxEvent.objects.filter(type="company.suspended").exists())

    def test_suspension_leaves_memberships_intact(self):
        # Suspension is reversible; destroying the team on the way in would make
        # reinstatement a rebuild. Access is refused by is_active instead.
        company_service.suspend_company(
            actor=self.staff, company=self.company, reason="Under review."
        )

        self.assertEqual(self.company.memberships.count(), 1)


class CompanyFileVisibilityTests(TestCase):
    """Company-visibility files gained a possible reader in P1."""

    def setUp(self):
        reset_scanner_cache()
        self.addCleanup(reset_scanner_cache)
        self.member = create_user()
        self.outsider = create_user()
        self.company = company_service.create_company(actor=self.member, name="Lumen")

        self.attachment = store(
            uploaded_file=file_factories.pdf_upload(),
            accepted_types=DOCUMENT_TYPES,
            visibility=Visibility.COMPANY,
            uploaded_by=self.member,
            scan=False,
        )
        self.attachment.owner_company = self.company
        self.attachment.scan_status = "clean"
        self.attachment.save(update_fields=["owner_company", "scan_status"])

    def test_a_member_can_read_a_company_file(self):
        from apps.files import policies

        self.assertTrue(policies.can_access(self.member, self.attachment))

    def test_an_outsider_cannot(self):
        from apps.files import policies

        self.assertFalse(policies.can_access(self.outsider, self.attachment))

    def test_a_company_file_with_no_owning_company_has_no_reader(self):
        from apps.files import policies

        self.attachment.owner_company = None
        self.attachment.save(update_fields=["owner_company"])

        self.assertFalse(policies.can_access(self.member, self.attachment))

    def test_a_removed_member_loses_access_to_company_files(self):
        from apps.files import policies

        self.company.memberships.update(deleted_at="2026-01-01T00:00:00Z")

        self.assertFalse(policies.can_access(self.member, self.attachment))
