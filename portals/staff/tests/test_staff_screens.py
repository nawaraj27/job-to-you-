"""
The staff surfaces (ADR-A5, SRS §2.3, §19.5).

Two properties carry this file.

**Staff authority is `StaffRole`, and nothing else.** Not `is_staff`, not a
company role, not being signed in. Asserted from the failing side for each: a
candidate gets a 404 (a staff screen should not exist as far as they are
concerned), and a support agent reaching a moderator screen gets a 403 — they
have an authorisation problem, not a mystery.

**Claim-and-lock actually locks.** Two reviewers must not work the same
submission, and a reviewer who walks away must not hold it forever.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StaffRole
from apps.candidates.tests.factories import create_profile
from apps.companies.models import CompanyVerification, VerificationState
from apps.companies.services import company as company_service
from apps.companies.tests.factories import company_with_owner


def make_staff(user, level: str) -> StaffRole:
    return StaffRole.objects.create(user=user, level=level)


def _attach_document(company, uploader) -> None:
    """A submission needs a document; the queue needs a submission."""
    from apps.companies.models import CompanyVerificationDocument
    from apps.files.constants import Visibility
    from apps.files.models import Attachment

    CompanyVerificationDocument.objects.create(
        verification=company.verification,
        kind="registration",
        attachment=Attachment.objects.create(
            storage_key=f"ab/cd/{company.slug}.pdf",
            filename="registration.pdf",
            mime="application/pdf",
            size=10,
            sha256="f" * 64,
            visibility=Visibility.COMPANY,
            owner_company=company,
        ),
        uploaded_by=uploader,
    )


class StaffFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.moderator = create_profile().user
        make_staff(self.moderator, "moderator")

        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        _attach_document(self.company, self.owner)
        company_service.submit_verification(actor=self.owner, company=self.company)

        self.client.force_login(self.moderator)
        self.url = reverse("staff:verification")


class AuthorityTests(StaffFixture):
    def test_a_candidate_gets_a_404_not_a_403(self):
        # A 403 confirms the screen exists, which is a fact a candidate has no
        # business learning.
        self.client.force_login(create_profile().user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_django_is_staff_is_not_authority(self):
        # Two systems answering "is this an operator" would eventually
        # disagree, and the one that mattered would be whichever the view asked.
        impostor = create_profile().user
        impostor.is_staff = True
        impostor.save(update_fields=["is_staff"])
        self.client.force_login(impostor)

        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_support_cannot_reach_the_moderator_queue(self):
        agent = create_profile().user
        make_staff(agent, "support")
        self.client.force_login(agent)

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_support_can_reach_the_operations_board(self):
        agent = create_profile().user
        make_staff(agent, "support")
        self.client.force_login(agent)

        self.assertEqual(self.client.get(reverse("staff:operations")).status_code, 200)

    def test_an_admin_can_reach_a_moderator_screen(self):
        # Levels are ordered: a check is "at least this", never a set.
        admin = create_profile().user
        make_staff(admin, "admin")
        self.client.force_login(admin)

        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class VerificationQueueTests(StaffFixture):
    def test_a_pending_submission_appears(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.company.name)

    def test_claiming_locks_it_to_one_reviewer(self):
        self.client.post(self.url, {"action": "claim", "company": self.company.slug})

        verification = CompanyVerification.objects.get(company=self.company)
        self.assertEqual(verification.claimed_by, self.moderator)
        self.assertTrue(verification.is_claimed)

    def test_a_second_reviewer_is_refused(self):
        self.client.post(self.url, {"action": "claim", "company": self.company.slug})

        other = create_profile().user
        make_staff(other, "moderator")
        self.client.force_login(other)
        self.client.post(self.url, {"action": "claim", "company": self.company.slug})

        verification = CompanyVerification.objects.get(company=self.company)
        self.assertEqual(verification.claimed_by, self.moderator)

    def test_an_expired_claim_returns_to_the_queue(self):
        # A reviewer who closes the tab must not hold an item forever.
        self.client.post(self.url, {"action": "claim", "company": self.company.slug})
        CompanyVerification.objects.filter(company=self.company).update(
            claimed_until=timezone.now() - dt.timedelta(minutes=1)
        )

        self.assertFalse(CompanyVerification.objects.get(company=self.company).is_claimed)
        self.assertEqual(company_service.expire_claims(), 1)

    def test_releasing_puts_it_back(self):
        self.client.post(self.url, {"action": "claim", "company": self.company.slug})
        self.client.post(self.url, {"action": "release", "company": self.company.slug})

        verification = CompanyVerification.objects.get(company=self.company)
        self.assertIsNone(verification.claimed_by)

    def test_verifying_goes_through_the_service_and_is_audited(self):
        from apps.audit.models import AuditLog

        self.client.post(self.url, {"action": "claim", "company": self.company.slug})
        self.client.post(
            self.url,
            {"action": "decide", "company": self.company.slug, "decision": "verified"},
        )

        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_state, VerificationState.VERIFIED)
        self.assertTrue(AuditLog.objects.filter(verb="company.verification_state_changed").exists())

    def test_rejecting_without_a_reason_is_refused(self):
        # The company has to know what to do next; "rejected" with no sentence
        # attached is a support ticket the platform wrote for itself.
        self.client.post(self.url, {"action": "claim", "company": self.company.slug})
        self.client.post(
            self.url,
            {"action": "decide", "company": self.company.slug, "decision": "rejected"},
        )

        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_state, VerificationState.PENDING)

    def test_an_unknown_company_does_not_500(self):
        response = self.client.post(self.url, {"action": "claim", "company": "no-such-company"})
        self.assertEqual(response.status_code, 302)

    def test_an_unknown_action_changes_nothing(self):
        self.client.post(self.url, {"action": "delete", "company": self.company.slug})

        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_state, VerificationState.PENDING)


class AnomalyQueueTests(StaffFixture):
    def setUp(self):
        super().setUp()
        self.url = reverse("staff:anomalies")

    def test_an_empty_queue_says_so(self):
        response = self.client.get(self.url)
        self.assertContains(response, "That is the normal state")

    def test_a_signal_appears(self):
        from apps.audit import services as audit

        audit.record(
            verb="candidate.export_anomaly",
            actor=self.owner,
            target=self.company,
            company=self.company,
            metadata={
                "kind": "large_export",
                "severity": "alert",
                "observed": 500,
                "threshold": 200,
            },
        )

        response = self.client.get(self.url)
        self.assertContains(response, "large_export")
        self.assertEqual(response.context["alerts"], 1)

    def test_the_window_can_be_narrowed_and_a_bad_one_falls_back(self):
        self.assertEqual(self.client.get(self.url, {"days": "1"}).context["days"], 1)
        self.assertEqual(self.client.get(self.url, {"days": "9999"}).context["days"], 7)

    def test_an_ordinary_audit_row_is_not_a_signal(self):
        from apps.audit import services as audit

        audit.record(verb="company.updated", actor=self.owner, target=self.company)
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["rows"]), 0)


class VerificationDocumentAccessTests(StaffFixture):
    """The queue showed a document count and no way to read a document.

    Verification documents are restricted data (SRS §15.3): opening one is
    moderator work and every read is logged twice — the file's access log and
    the audit log.
    """

    def setUp(self):
        super().setUp()
        from apps.companies.models import CompanyVerificationDocument
        from apps.files.constants import DOCUMENT_TYPES, Visibility
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
        attachment.scan_status = "clean"
        attachment.save(update_fields=["owner_company", "scan_status"])
        self.document = CompanyVerificationDocument.objects.create(
            verification=self.company.verification,
            kind="registration",
            attachment=attachment,
            uploaded_by=self.owner,
        )
        self.doc_url = reverse("staff:verification_document", args=[self.document.pk])

    def _open(self):
        response = self.client.get(self.doc_url)
        # Close the file handle only. `response.close()` fires
        # `request_finished`, which closes the test's database connection.
        stream = getattr(response, "file_to_stream", None)
        if stream is not None:
            stream.close()
        return response

    def test_a_moderator_opens_a_document_and_the_read_is_logged(self):
        from apps.audit.models import AuditLog
        from apps.files.models import AttachmentAccess

        self.assertEqual(self._open().status_code, 200)
        self.assertTrue(
            AttachmentAccess.objects.filter(
                attachment=self.document.attachment,
                actor=self.moderator,
                purpose="verification_review",
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                verb="company.verification_document_opened", actor=self.moderator
            ).exists()
        )

    def test_support_and_candidates_cannot_open_one_and_nothing_is_logged(self):
        from apps.files.models import AttachmentAccess

        agent = create_profile().user
        make_staff(agent, "support")
        self.client.force_login(agent)
        self.assertEqual(self._open().status_code, 403)

        self.client.force_login(create_profile().user)
        self.assertEqual(self._open().status_code, 404)

        self.client.force_login(self.owner)  # a member of the company, not staff
        self.assertEqual(self._open().status_code, 404)

        self.assertFalse(AttachmentAccess.objects.exists())

    def test_a_document_still_being_scanned_cannot_be_opened(self):
        self.document.attachment.scan_status = "pending"
        self.document.attachment.save(update_fields=["scan_status"])
        self.assertEqual(self._open().status_code, 404)

    def test_an_unknown_document_is_404(self):
        response = self.client.get(reverse("staff:verification_document", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_the_queue_links_each_openable_document_by_name(self):
        page = self.client.get(self.url)
        self.assertContains(page, self.doc_url)
        self.assertContains(page, "Company registration certificate")

    def test_the_decision_buttons_read_as_words(self):
        self.client.post(self.url, {"action": "claim", "company": self.company.slug})
        page = self.client.get(self.url)
        self.assertContains(page, "More information needed")
        self.assertNotContains(page, "More_Info")


class ClaimEnforcementScreenTests(StaffFixture):
    """The page hid the buttons; the service did not refuse the POST."""

    def _as_second_moderator(self):
        self.client.post(self.url, {"action": "claim", "company": self.company.slug})
        other = create_profile().user
        make_staff(other, "moderator")
        self.client.force_login(other)

    def test_a_second_moderator_cannot_decide_over_a_claim(self):
        self._as_second_moderator()
        self.client.post(
            self.url, {"action": "decide", "company": self.company.slug, "decision": "verified"}
        )
        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_state, VerificationState.PENDING)

    def test_a_second_moderator_cannot_release_a_claim(self):
        self._as_second_moderator()
        self.client.post(self.url, {"action": "release", "company": self.company.slug})
        verification = CompanyVerification.objects.get(company=self.company)
        self.assertEqual(verification.claimed_by, self.moderator)

    def test_nobody_can_decide_without_claiming(self):
        self.client.post(
            self.url, {"action": "decide", "company": self.company.slug, "decision": "verified"}
        )
        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_state, VerificationState.PENDING)


class AnomalyLabelTests(StaffFixture):
    def test_a_signal_reads_as_words_not_a_code(self):
        from apps.audit import services as audit

        audit.record(
            verb="candidate.view_anomaly",
            actor=self.owner,
            target=self.company,
            company=self.company,
            metadata={
                "kind": "view_volume",
                "severity": "alert",
                "observed": 250,
                "threshold": 200,
            },
        )
        response = self.client.get(reverse("staff:anomalies"))
        self.assertContains(response, "Candidate profiles opened today")
        self.assertNotContains(response, "view_volume")


class OperationsBoardTests(StaffFixture):
    def setUp(self):
        super().setUp()
        self.url = reverse("staff:operations")

    def test_the_board_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Outbox waiting")

    def test_every_retention_promise_is_listed(self):
        from apps.common import retention

        response = self.client.get(self.url)
        self.assertEqual(len(response.context["retention"]), len(retention.SCHEDULE))

    def test_a_scheduled_erasure_is_counted(self):
        from apps.accounts.services import erasure

        candidate = create_profile().user
        erasure.request_erasure(user=candidate, actor=candidate, confirmation=erasure.CONFIRMATION)

        response = self.client.get(self.url)
        self.assertEqual(response.context["erasures_pending"], 1)

    def test_an_expired_flag_is_surfaced(self):
        from apps.core.models import FeatureFlag

        FeatureFlag.objects.create(
            key="old_experiment",
            is_enabled=True,
            expires_on=timezone.localdate() - dt.timedelta(days=1),
        )

        response = self.client.get(self.url)
        self.assertIn("old_experiment", response.context["expired_flags"])
