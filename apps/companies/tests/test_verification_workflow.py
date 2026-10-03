"""
The verification workflow end to end: register, submit, review, decide.

The pieces are each covered elsewhere — the state machine in
`test_verification.py`, decisions D-02/D-04/D-07 in
`test_verification_decisions.py`, permissions in `test_permissions.py`. What
is asserted here is the *join*: that a company registered through the ordinary
path arrives at a reviewer with what the reviewer needs, that an incomplete
submission is visible as incomplete to both sides rather than being silently
treated as enough, and that no route from "registered" to "verified" exists
that does not pass through a moderator's decision.

Every test goes through the real screens where a screen is what a person would
use, because the question "can a company become verified without a reviewer"
is not answerable from the service layer alone.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import StaffRole
from apps.accounts.tests.factories import create_staff, create_user
from apps.audit.models import AuditLog
from apps.common.errors import NotAllowed
from apps.companies.models import (
    CompanyMembership,
    CompanyVerificationDecision,
    CompanyVerificationDocument,
    VerificationState,
)
from apps.companies.permissions import Role
from apps.companies.services import company as company_service
from apps.companies.tests.factories import add_member
from apps.files.constants import ScanStatus, Visibility
from apps.files.models import Attachment
from apps.notifications.models import OutboxEvent


def attach(company, uploader, *, kind: str, scan: str = ScanStatus.CLEAN, name: str = "") -> None:
    """One verification document, straight onto the record.

    The upload path itself is asserted in `test_onboarding_loop_regressions`;
    these tests are about what happens to a submission once the files are on
    it, and going through the storage service here would only add a scanner.
    """
    filename = name or f"{kind}.pdf"
    CompanyVerificationDocument.objects.create(
        verification=company.verification,
        kind=kind,
        attachment=Attachment.objects.create(
            storage_key=f"ab/cd/{company.slug}-{filename}",
            filename=filename,
            mime="application/pdf",
            size=10,
            sha256="f" * 64,
            visibility=Visibility.COMPANY,
            owner_company=company,
            scan_status=scan,
        ),
        uploaded_by=uploader,
    )


class WorkflowFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.owner = create_user()
        self.company = company_service.create_company(actor=self.owner, name="Kora Traders")
        self.membership = CompanyMembership.objects.get(company=self.company, user=self.owner)

        self.reviewer = create_staff()
        StaffRole.objects.create(user=self.reviewer, level="moderator")

    def submit_complete(self):
        attach(self.company, self.owner, kind="registration")
        attach(self.company, self.owner, kind="pan_vat")
        return company_service.submit_verification(actor=self.owner, company=self.company)

    def decide(self, decision, reason=""):
        company_service.claim_verification(actor=self.reviewer, company=self.company)
        return company_service.review_verification(
            actor=self.reviewer, company=self.company, decision=decision, reason=reason
        )


class RegistrationTests(WorkflowFixture):
    def test_a_new_company_is_not_verified_and_has_not_submitted(self):
        self.assertEqual(
            self.company.verification.state,
            VerificationState.NOT_SUBMITTED,
        )
        self.assertEqual(self.company.verification.attempt, 0)
        self.assertEqual(self.company.verification_state, VerificationState.NOT_SUBMITTED)
        self.assertFalse(self.company.verification.decisions.exists())

    def test_registration_alone_never_reaches_the_queue(self):
        from apps.companies import selectors

        self.assertNotIn(
            self.company.pk, [row.company_id for row in selectors.verification_queue()]
        )


class CompletenessTests(WorkflowFixture):
    def test_a_new_company_is_missing_everything_a_reviewer_needs(self):
        self.assertEqual(
            company_service.missing_document_kinds(self.company.verification),
            ["registration", "pan_vat"],
        )
        self.assertFalse(company_service.submission_is_complete(self.company.verification))

    def test_one_certificate_of_two_is_still_incomplete(self):
        attach(self.company, self.owner, kind="registration")

        self.assertEqual(
            company_service.missing_document_kinds(self.company.verification), ["pan_vat"]
        )

    def test_both_certificates_complete_the_submission(self):
        attach(self.company, self.owner, kind="registration")
        attach(self.company, self.owner, kind="pan_vat")

        self.assertTrue(company_service.submission_is_complete(self.company.verification))

    def test_a_file_a_reviewer_cannot_open_does_not_count(self):
        """Quarantined is not "attached": as evidence it is not there.

        A company that uploaded an infected scan and saw it counted would be
        waiting on a review whose only possible outcome is being asked for
        the same certificate again.
        """
        attach(self.company, self.owner, kind="registration")
        attach(self.company, self.owner, kind="pan_vat", scan=ScanStatus.INFECTED)

        self.assertEqual(
            company_service.missing_document_kinds(self.company.verification), ["pan_vat"]
        )

    def test_a_file_still_being_scanned_counts_as_here(self):
        attach(self.company, self.owner, kind="registration")
        attach(self.company, self.owner, kind="pan_vat", scan=ScanStatus.PENDING)

        self.assertTrue(company_service.submission_is_complete(self.company.verification))

    def test_the_checklist_labels_every_required_kind(self):
        attach(self.company, self.owner, kind="registration")
        checklist = company_service.document_checklist(self.company.verification)

        self.assertEqual([item["kind"] for item in checklist], ["registration", "pan_vat"])
        self.assertEqual([item["present"] for item in checklist], [True, False])
        self.assertEqual(checklist[1]["label"], "PAN or VAT certificate")

    def test_an_incomplete_submission_does_not_become_verified_on_its_own(self):
        attach(self.company, self.owner, kind="registration")
        company_service.submit_verification(actor=self.owner, company=self.company)
        self.company.refresh_from_db()

        self.assertEqual(self.company.verification.state, VerificationState.PENDING)
        self.assertNotEqual(self.company.verification_state, VerificationState.VERIFIED)


class ReviewerSeesTheSubmissionTests(WorkflowFixture):
    def test_the_queue_names_what_the_company_did_not_send(self):
        attach(self.company, self.owner, kind="registration")
        company_service.submit_verification(actor=self.owner, company=self.company)
        self.client.force_login(self.reviewer)

        response = self.client.get(reverse("staff:verification"))

        self.assertContains(response, "Kora Traders")
        self.assertContains(response, "registration.pdf")
        self.assertContains(response, "Not sent:")
        self.assertContains(response, "PAN or VAT certificate")

    def test_a_complete_submission_is_not_reported_as_missing_anything(self):
        self.submit_complete()
        self.client.force_login(self.reviewer)

        response = self.client.get(reverse("staff:verification"))

        self.assertContains(response, "pan_vat.pdf")
        self.assertNotContains(response, "Not sent:")

    def _stored_document(self):
        """A document with real bytes behind it, so `serve` has a file."""
        from apps.files.constants import DOCUMENT_TYPES
        from apps.files.scanning import reset_scanner_cache
        from apps.files.services import store
        from apps.files.tests import factories as file_factories

        reset_scanner_cache()
        self.addCleanup(reset_scanner_cache)
        attachment = store(
            uploaded_file=file_factories.pdf_upload("registration.pdf"),
            accepted_types=DOCUMENT_TYPES,
            visibility=Visibility.COMPANY,
            uploaded_by=self.owner,
            scan=False,
        )
        attachment.owner_company = self.company
        attachment.scan_status = ScanStatus.CLEAN
        attachment.save(update_fields=["owner_company", "scan_status"])
        return CompanyVerificationDocument.objects.create(
            verification=self.company.verification,
            kind="registration",
            attachment=attachment,
            uploaded_by=self.owner,
        )

    def _open(self, document):
        response = self.client.get(reverse("staff:verification_document", args=[document.pk]))
        # Close the file handle only: `response.close()` fires
        # `request_finished`, which closes the test's database connection.
        stream = getattr(response, "file_to_stream", None)
        if stream is not None:
            stream.close()
        return response

    def test_a_reviewer_can_open_a_document_and_the_read_is_recorded(self):
        document = self._stored_document()
        attach(self.company, self.owner, kind="pan_vat")
        company_service.submit_verification(actor=self.owner, company=self.company)
        self.client.force_login(self.reviewer)

        self.assertEqual(self._open(document).status_code, 200)
        self.assertTrue(
            AuditLog.objects.filter(
                verb="company.verification_document_opened", actor=self.reviewer
            ).exists()
        )

    def test_being_django_staff_is_not_enough_to_reach_the_queue(self):
        """`is_staff` opens the Django admin, not the verification queue.

        The project's own staff levels are the permission model, and an
        account that is only `is_staff` has no level at all: the staff
        surface is not even acknowledged to it.
        """
        self.client.force_login(create_staff())

        self.assertEqual(self.client.get(reverse("staff:verification")).status_code, 404)

    def test_a_signed_in_employer_cannot_reach_the_queue(self):
        self.client.force_login(self.owner)

        self.assertEqual(self.client.get(reverse("staff:verification")).status_code, 404)

    def test_a_support_agent_cannot_open_a_verification_document(self):
        document = self._stored_document()
        agent = create_staff()
        StaffRole.objects.create(user=agent, level="support")
        self.client.force_login(agent)

        self.assertEqual(self._open(document).status_code, 403)


class DecisionTests(WorkflowFixture):
    def test_approving_verifies_the_company_and_records_the_decision(self):
        self.submit_complete()
        verification = self.decide(VerificationState.VERIFIED)
        self.company.refresh_from_db()

        self.assertEqual(verification.state, VerificationState.VERIFIED)
        self.assertEqual(self.company.verification_state, VerificationState.VERIFIED)
        decision = CompanyVerificationDecision.objects.get()
        self.assertEqual(decision.state, VerificationState.VERIFIED)
        self.assertEqual(decision.reviewed_by, self.reviewer)
        self.assertEqual(decision.attempt, 1)
        self.assertEqual(
            sorted(entry["kind"] for entry in decision.documents), ["pan_vat", "registration"]
        )

    def test_more_information_keeps_the_attempt_and_the_reason(self):
        self.submit_complete()
        verification = self.decide(
            VerificationState.MORE_INFO, reason="Send the PAN certificate, not the VAT receipt."
        )

        self.assertEqual(verification.state, VerificationState.MORE_INFO)
        self.assertEqual(verification.attempt, 1)
        self.assertEqual(verification.reason, "Send the PAN certificate, not the VAT receipt.")
        self.assertEqual(CompanyVerificationDecision.objects.get().attempt, 1)

    def test_answering_a_request_for_more_is_the_same_attempt(self):
        self.submit_complete()
        self.decide(VerificationState.MORE_INFO, reason="The PAN certificate is missing.")
        attach(self.company, self.owner, kind="pan_vat", name="pan-second.pdf")

        verification = company_service.submit_verification(actor=self.owner, company=self.company)

        self.assertEqual(verification.state, VerificationState.PENDING)
        self.assertEqual(verification.attempt, 1)
        # The earlier decision is untouched by the resubmission (D-02).
        self.assertEqual(CompanyVerificationDecision.objects.count(), 1)
        self.assertEqual(
            CompanyVerificationDecision.objects.get().reason, "The PAN certificate is missing."
        )

    def test_rejecting_does_not_verify_and_keeps_its_reason(self):
        self.submit_complete()
        verification = self.decide(
            VerificationState.REJECTED, reason="The certificate belongs to another company."
        )
        self.company.refresh_from_db()

        self.assertEqual(verification.state, VerificationState.REJECTED)
        self.assertNotEqual(self.company.verification_state, VerificationState.VERIFIED)
        self.assertEqual(
            CompanyVerificationDecision.objects.get().reason,
            "The certificate belongs to another company.",
        )

    def test_every_decision_notifies_the_company(self):
        self.submit_complete()
        self.decide(VerificationState.MORE_INFO, reason="One more certificate, please.")

        event = OutboxEvent.objects.filter(type="company.verification_reviewed").latest("id")
        self.assertEqual(event.payload["to_state"], VerificationState.MORE_INFO)
        self.assertEqual(event.payload["outcome"], "needs more information")
        self.assertEqual(event.payload["reason"], "One more certificate, please.")

    def test_nobody_without_the_moderator_level_can_decide(self):
        self.submit_complete()
        company_service.claim_verification(actor=self.reviewer, company=self.company)

        agent = create_staff()
        StaffRole.objects.create(user=agent, level="support")

        # A company owner and a plain `is_staff` account are not offered the
        # staff surface at all; a support agent is staff and is refused.
        for actor, expected in ((self.owner, 404), (create_staff(), 404), (agent, 403)):
            with self.subTest(actor=actor.pk):
                self.client.force_login(actor)
                response = self.client.post(
                    reverse("staff:verification"),
                    {
                        "company": self.company.slug,
                        "action": "decide",
                        "decision": VerificationState.VERIFIED,
                    },
                )
                self.assertEqual(response.status_code, expected)

        self.company.refresh_from_db()
        self.assertEqual(self.company.verification.state, VerificationState.PENDING)

    def test_a_reviewer_who_belongs_to_the_company_cannot_decide(self):
        """D-04, asserted here because it is the one hole that verifies a
        company without an independent review."""
        self.submit_complete()
        add_member(self.company, self.reviewer, role=Role.RECRUITER)

        with self.assertRaises(NotAllowed):
            company_service.claim_verification(actor=self.reviewer, company=self.company)

        self.company.refresh_from_db()
        self.assertEqual(self.company.verification.state, VerificationState.PENDING)


class WhatTheCompanySeesTests(WorkflowFixture):
    def setUp(self):
        super().setUp()
        self.url = reverse("employer:verification", args=[self.company.slug])
        self.client.force_login(self.owner)

    def test_a_new_company_is_told_which_certificates_are_wanted(self):
        response = self.client.get(self.url)

        self.assertContains(response, "Not submitted")
        self.assertContains(response, "Company registration certificate")
        self.assertContains(response, "PAN or VAT certificate")
        self.assertContains(response, "still needed", count=2)

    def test_an_attached_certificate_stops_being_listed_as_needed(self):
        attach(self.company, self.owner, kind="registration")

        response = self.client.get(self.url)

        self.assertContains(response, "attached")
        self.assertContains(response, "still needed", count=1)

    def test_an_incomplete_submission_is_warned_about_rather_than_blocked(self):
        attach(self.company, self.owner, kind="registration")

        response = self.client.get(self.url)

        self.assertContains(response, "Not everything a reviewer looks for is here")
        self.assertContains(response, "Still needed: PAN or VAT certificate.")
        self.assertNotContains(response, "disabled aria-describedby")

    def test_a_complete_submission_is_not_warned_about(self):
        attach(self.company, self.owner, kind="registration")
        attach(self.company, self.owner, kind="pan_vat")

        response = self.client.get(self.url)

        self.assertNotContains(response, "Not everything a reviewer looks for is here")

    def test_while_waiting_the_company_is_told_what_is_still_outstanding(self):
        attach(self.company, self.owner, kind="registration")
        company_service.submit_verification(actor=self.owner, company=self.company)

        response = self.client.get(self.url)

        self.assertContains(response, "In review")
        self.assertContains(response, "A reviewer will also look for:")

    def test_a_request_for_more_information_says_what_to_do_next(self):
        attach(self.company, self.owner, kind="registration")
        company_service.submit_verification(actor=self.owner, company=self.company)
        company_service.claim_verification(actor=self.reviewer, company=self.company)
        company_service.review_verification(
            actor=self.reviewer,
            company=self.company,
            decision=VerificationState.MORE_INFO,
            reason="Send the PAN certificate.",
        )

        response = self.client.get(self.url)

        self.assertContains(response, "More information needed")
        self.assertContains(response, "Send the PAN certificate.")
        self.assertContains(response, "Add what the reviewer asked for")
        # And the screen is editable again, so they can actually do it.
        self.assertContains(response, "Upload document")

    def test_the_verified_company_sees_that_and_its_history(self):
        attach(self.company, self.owner, kind="registration")
        attach(self.company, self.owner, kind="pan_vat")
        company_service.submit_verification(actor=self.owner, company=self.company)
        company_service.claim_verification(actor=self.reviewer, company=self.company)
        company_service.review_verification(
            actor=self.reviewer, company=self.company, decision=VerificationState.VERIFIED
        )

        response = self.client.get(self.url)

        self.assertContains(response, "Verified")
        self.assertContains(response, "Earlier decisions")
        self.assertContains(response, "Attempt 1")
