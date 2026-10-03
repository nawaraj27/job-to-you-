"""
Owner decisions D-02, D-04 and D-07 on company verification.

D-02: a rejected company may apply again thirty days after the decision. The
      rejection — reason, reviewer, time, audit rows — is never overwritten,
      and the new submission is a distinguishable attempt.
D-04: a reviewer who is a member of the company, holds a pending invitation to
      it, or sent the submission may neither claim nor decide it. No override.
D-07: domain proof is optional; documents are required.
"""

from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StaffRole
from apps.accounts.tests.factories import create_staff, create_user
from apps.audit.models import AuditLog
from apps.common.errors import InvalidState, NotAllowed
from apps.companies.models import (
    CompanyMembership,
    CompanyVerificationDecision,
    CompanyVerificationDocument,
    VerificationState,
)
from apps.companies.permissions import Role
from apps.companies.services import company as company_service
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member
from apps.files.constants import Visibility
from apps.files.models import Attachment


def _document(company, uploader, name="registration.pdf") -> CompanyVerificationDocument:
    return CompanyVerificationDocument.objects.create(
        verification=company.verification,
        kind="registration",
        attachment=Attachment.objects.create(
            storage_key=f"ab/cd/{company.slug}-{name}",
            filename=name,
            mime="application/pdf",
            size=10,
            sha256="f" * 64,
            visibility=Visibility.COMPANY,
            owner_company=company,
            scan_status="clean",
        ),
        uploaded_by=uploader,
    )


class _Fixture(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.reviewer = create_staff()
        StaffRole.objects.create(user=self.reviewer, level="moderator")
        self.company = company_service.create_company(actor=self.owner, name="Kora Traders")
        self.membership = CompanyMembership.objects.get(company=self.company, user=self.owner)
        _document(self.company, self.owner)

    def _reject(self, reason="The registration number does not match the certificate."):
        company_service.submit_verification(actor=self.owner, company=self.company)
        company_service.claim_verification(actor=self.reviewer, company=self.company)
        return company_service.review_verification(
            actor=self.reviewer,
            company=self.company,
            decision=VerificationState.REJECTED,
            reason=reason,
        )

    def _later(self, days: int):
        return mock.patch(
            "django.utils.timezone.now", return_value=timezone.now() + dt.timedelta(days=days)
        )


class RejectedReapplicationTests(_Fixture):
    def test_before_thirty_days_a_new_submission_is_refused(self):
        self._reject()
        with self._later(29), self.assertRaises(InvalidState) as caught:
            company_service.submit_verification(actor=self.owner, company=self.company)
        self.assertIn("apply again", caught.exception.message)
        self.company.verification.refresh_from_db()
        self.assertEqual(self.company.verification.state, VerificationState.REJECTED)

    def test_documents_are_frozen_before_thirty_days(self):
        self._reject()
        with self._later(10), self.assertRaises(InvalidState):
            company_service.remove_verification_document(
                actor=self.owner,
                membership=self.membership,
                company=self.company,
                document_id=self.company.verification.documents.get().pk,
            )

    def test_at_thirty_days_the_company_edits_and_submits_a_new_attempt(self):
        rejected = self._reject()
        first_reviewed_at = rejected.reviewed_at
        old_document = self.company.verification.documents.get()

        with self._later(30):
            # Replace the document the reviewer rejected, then send again.
            company_service.remove_verification_document(
                actor=self.owner,
                membership=self.membership,
                company=self.company,
                document_id=old_document.pk,
            )
            _document(self.company, self.owner, name="registration-corrected.pdf")
            verification = company_service.submit_verification(
                actor=self.owner, company=self.company
            )

        self.assertEqual(verification.state, VerificationState.PENDING)
        self.assertEqual(verification.attempt, 2)
        self.assertEqual(verification.submitted_by, self.owner)

        # The rejection is still on record, word for word.
        decision = CompanyVerificationDecision.objects.get(verification=verification)
        self.assertEqual(decision.attempt, 1)
        self.assertEqual(decision.state, VerificationState.REJECTED)
        self.assertEqual(decision.reviewed_by, self.reviewer)
        self.assertEqual(decision.decided_at, first_reviewed_at)
        self.assertIn("registration number", decision.reason)
        self.assertEqual(decision.documents[0]["filename"], "registration.pdf")
        # The rejected file is kept as evidence, only detached from attempt 2.
        old_document.attachment.refresh_from_db()
        self.assertIsNone(old_document.attachment.deleted_at)

        # The audit trail tells the attempts apart.
        transitions = list(
            AuditLog.objects.filter(verb="company.verification_state_changed")
            .order_by("created_at", "id")
            .values_list("metadata", flat=True)
        )
        self.assertEqual(
            [(row["to"], row["attempt"]) for row in transitions],
            [("pending", 1), ("rejected", 1), ("pending", 2)],
        )

    def test_the_second_decision_adds_to_the_history_and_overwrites_nothing(self):
        self._reject()
        with self._later(31):
            company_service.submit_verification(actor=self.owner, company=self.company)
            company_service.claim_verification(actor=self.reviewer, company=self.company)
            company_service.review_verification(
                actor=self.reviewer, company=self.company, decision=VerificationState.VERIFIED
            )

        history = list(
            CompanyVerificationDecision.objects.filter(verification__company=self.company).order_by(
                "attempt"
            )
        )
        self.assertEqual(
            [(d.attempt, d.state) for d in history],
            [(1, VerificationState.REJECTED), (2, VerificationState.VERIFIED)],
        )
        self.assertIn("registration number", history[0].reason)

    def test_more_information_is_the_same_attempt(self):
        company_service.submit_verification(actor=self.owner, company=self.company)
        company_service.claim_verification(actor=self.reviewer, company=self.company)
        company_service.review_verification(
            actor=self.reviewer,
            company=self.company,
            decision=VerificationState.MORE_INFO,
            reason="Add the PAN certificate.",
        )
        verification = company_service.submit_verification(actor=self.owner, company=self.company)
        self.assertEqual(verification.attempt, 1)

    def test_a_suspended_company_cannot_reapply(self):
        self._reject()
        company_service.suspend_company(actor=self.reviewer, company=self.company, reason="Fraud")
        with self._later(40), self.assertRaises(NotAllowed):
            company_service.submit_verification(actor=self.owner, company=self.company)

    def test_the_screen_gives_the_date_then_offers_the_new_attempt(self):
        self._reject()
        self.client.force_login(self.owner)
        url = reverse("employer:verification", args=[self.company.slug])

        page = self.client.get(url)
        self.company.verification.refresh_from_db()
        opens = timezone.localtime(self.company.verification.reviewed_at + dt.timedelta(days=30))
        self.assertContains(page, "You can apply again from")
        self.assertContains(page, f"{opens.day} {opens:%b %Y}")
        self.assertNotContains(page, "Submit for review")
        self.assertContains(page, "Earlier decisions")

        # Thirty days on. Moved in the database rather than on the clock: a
        # clock thirty days ahead also expires the owner's MFA grace period.
        verification = self.company.verification
        verification.reviewed_at = timezone.now() - dt.timedelta(days=30)
        verification.save(update_fields=["reviewed_at"])

        page = self.client.get(url)
        self.assertContains(page, "apply again")
        self.assertContains(page, "Submit for review")
        self.assertContains(page, "This starts attempt 2")
        response = self.client.post(url, {"action": "submit"})
        self.assertEqual(response.status_code, 302)
        self.company.verification.refresh_from_db()
        self.assertEqual(self.company.verification.attempt, 2)

    def test_an_early_post_is_refused_with_the_date(self):
        self._reject()
        self.client.force_login(self.owner)
        url = reverse("employer:verification", args=[self.company.slug])
        response = self.client.post(url, {"action": "submit"}, follow=True)
        self.assertContains(response, "You can apply again from")
        self.company.verification.refresh_from_db()
        self.assertEqual(self.company.verification.state, VerificationState.REJECTED)


class ReviewerConflictTests(_Fixture):
    """D-04: members, invitees and the submitter never claim or decide."""

    def setUp(self):
        super().setUp()
        company_service.submit_verification(actor=self.owner, company=self.company)

    def _assert_refused(self, reviewer, conflict: str):
        with self.assertRaises(NotAllowed):
            company_service.claim_verification(actor=reviewer, company=self.company)
        self.company.verification.refresh_from_db()
        self.assertIsNone(self.company.verification.claimed_by)
        row = AuditLog.objects.filter(
            verb="company.verification_conflict_refused", actor=reviewer
        ).latest("created_at")
        self.assertEqual(row.metadata["conflict"], conflict)
        self.assertEqual(row.metadata["operation"], "claim")

    def test_an_active_member_is_refused(self):
        staff_member = create_staff()
        StaffRole.objects.create(user=staff_member, level="superadmin")
        add_member(self.company, staff_member, Role.VIEWER)
        self._assert_refused(staff_member, "member")

    def test_a_pending_invitee_is_refused(self):
        invited = create_staff(email="invited.reviewer@example.com")
        StaffRole.objects.create(user=invited, level="moderator")
        membership_service.invite(
            actor=self.owner,
            actor_membership=self.membership,
            company=self.company,
            email="Invited.Reviewer@example.com",
            role=Role.RECRUITER,
        )
        self._assert_refused(invited, "invited")

    def test_the_submitter_is_refused(self):
        # A staff member who submitted for the company and later left it.
        submitter = create_staff()
        StaffRole.objects.create(user=submitter, level="moderator")
        self.company.verification.submitted_by = submitter
        self.company.verification.save(update_fields=["submitted_by"])
        self._assert_refused(submitter, "submitter")

    def test_a_conflict_arising_after_the_claim_still_blocks_the_decision(self):
        company_service.claim_verification(actor=self.reviewer, company=self.company)
        add_member(self.company, self.reviewer, Role.VIEWER)

        with self.assertRaises(NotAllowed):
            company_service.review_verification(
                actor=self.reviewer, company=self.company, decision=VerificationState.VERIFIED
            )
        self.company.verification.refresh_from_db()
        self.assertEqual(self.company.verification.state, VerificationState.PENDING)
        self.assertFalse(CompanyVerificationDecision.objects.exists())
        self.assertTrue(
            AuditLog.objects.filter(
                verb="company.verification_conflict_refused",
                metadata__operation="decide",
                metadata__conflict="member",
            ).exists()
        )

    def test_an_unrelated_reviewer_claims_and_decides(self):
        company_service.claim_verification(actor=self.reviewer, company=self.company)
        company_service.review_verification(
            actor=self.reviewer, company=self.company, decision=VerificationState.VERIFIED
        )
        self.company.refresh_from_db()
        self.assertTrue(self.company.is_verified)
        self.assertFalse(
            AuditLog.objects.filter(verb="company.verification_conflict_refused").exists()
        )

    def test_a_revoked_invitation_or_a_removed_membership_is_no_conflict(self):
        other = create_staff(email="former@example.com")
        StaffRole.objects.create(user=other, level="moderator")
        invite, _token = membership_service.invite(
            actor=self.owner,
            actor_membership=self.membership,
            company=self.company,
            email="former@example.com",
            role=Role.RECRUITER,
        )
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at"])
        former = add_member(self.company, other, Role.VIEWER)
        former.deleted_at = timezone.now()
        former.save(update_fields=["deleted_at"])

        company_service.claim_verification(actor=other, company=self.company)

    def test_the_queue_says_why_and_offers_no_claim(self):
        add_member(self.company, self.reviewer, Role.VIEWER)
        self.client.force_login(self.reviewer)

        page = self.client.get(reverse("staff:verification"))

        self.assertContains(
            page, "You cannot review this company: you are a member of this company"
        )
        self.assertNotContains(page, "Claim and review")

        response = self.client.post(
            reverse("staff:verification"),
            {"action": "claim", "company": self.company.slug},
            follow=True,
        )
        self.assertContains(response, "Another reviewer needs to take it")

    def test_the_queue_labels_a_second_attempt_with_the_earlier_rejection(self):
        company_service.claim_verification(actor=self.reviewer, company=self.company)
        company_service.review_verification(
            actor=self.reviewer,
            company=self.company,
            decision=VerificationState.REJECTED,
            reason="Certificate expired.",
        )
        with self._later(30):
            company_service.submit_verification(actor=self.owner, company=self.company)
        second = create_staff()
        StaffRole.objects.create(user=second, level="moderator")
        self.client.force_login(second)

        page = self.client.get(reverse("staff:verification"))

        self.assertContains(page, "Attempt 2")
        self.assertContains(page, "Certificate expired.")
        self.assertContains(page, "Claim and review")


class DomainProofOptionalTests(_Fixture):
    """D-07: documents are required; domain proof is not."""

    def test_a_company_without_domain_proof_can_submit(self):
        self.assertFalse(self.company.domains.filter(verified_at__isnull=False).exists())
        verification = company_service.submit_verification(actor=self.owner, company=self.company)
        self.assertEqual(verification.state, VerificationState.PENDING)

    def test_documents_are_still_required(self):
        self.company.verification.documents.all().delete()
        with self.assertRaises(InvalidState):
            company_service.submit_verification(actor=self.owner, company=self.company)

    def test_a_free_mail_website_cannot_be_proven(self):
        from apps.companies.services import domains

        self.company.website = "https://gmail.com"
        self.company.save(update_fields=["website"])
        self.assertEqual(domains.website_domain(self.company), "")

    def test_the_reviewer_sees_whether_the_domain_is_proven(self):
        self.client.force_login(self.reviewer)
        company_service.submit_verification(actor=self.owner, company=self.company)
        page = self.client.get(reverse("staff:verification"))
        self.assertContains(page, "domain not proven")

    def test_the_employer_screen_calls_domain_proof_optional(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("employer:verification", args=[self.company.slug]))
        self.assertContains(page, "optional")
