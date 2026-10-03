"""
Employer onboarding, team and verification defects reproduced on the running
product (section loop, 2026-09-14).

  * verification could never be submitted: the screen had no upload, and the
    service refused a submission without a document;
  * a website of "not a url" was stored and shown as the company's address;
  * a blank email or an unknown role produced an invitation;
  * an open invitation could not be withdrawn;
  * any moderator could decide or release an item another moderator had
    claimed, or decide one nobody had claimed;
  * nobody at the company was told a reviewer's decision, including "more
    information needed";
  * two owners removing each other at the same moment could leave a company
    with no owner.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_staff, create_user
from apps.audit.models import AuditLog
from apps.common.errors import Conflict, DomainError, InvalidState, NotAllowed, NotFound
from apps.companies.models import (
    CompanyInvite,
    CompanyMembership,
    CompanyVerification,
    VerificationState,
)
from apps.companies.permissions import Grant, Role
from apps.companies.services import company as company_service
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member, company_with_owner
from apps.files.constants import Visibility
from apps.files.scanning import reset_scanner_cache
from apps.files.tests import factories as file_factories
from apps.notifications.models import Notification
from apps.notifications.services import fanout, outbox


def deliver() -> None:
    for _ in range(10):
        if not outbox.drain(500):
            break
    for _ in range(10):
        if not fanout.run_due(limit=500).get("chunks"):
            break


class CompanyFixture(TestCase):
    def setUp(self):
        reset_scanner_cache()
        self.addCleanup(reset_scanner_cache)
        self.owner = create_user()
        self.company = company_service.create_company(actor=self.owner, name="Himal Traders")
        self.owner_membership = CompanyMembership.objects.get(company=self.company, user=self.owner)

    def upload(self, *, actor=None, membership=None, kind="registration", uploaded_file="default"):
        return company_service.add_verification_document(
            actor=actor or self.owner,
            membership=membership or self.owner_membership,
            company=self.company,
            kind=kind,
            uploaded_file=file_factories.pdf_upload("registration.pdf")
            if uploaded_file == "default"
            else uploaded_file,
        )


class VerificationDocumentTests(CompanyFixture):
    def test_an_owner_can_attach_a_document_and_then_submit(self):
        document = self.upload()

        self.assertEqual(document.attachment.visibility, Visibility.COMPANY)
        self.assertEqual(document.attachment.owner_company, self.company)
        verification = company_service.submit_verification(actor=self.owner, company=self.company)
        self.assertEqual(verification.state, VerificationState.PENDING)

    def test_attaching_is_audited(self):
        self.upload()
        self.assertTrue(
            AuditLog.objects.filter(verb="company.verification_document_added").exists()
        )

    def test_a_recruiter_cannot_attach(self):
        recruiter = create_user()
        membership = add_member(self.company, recruiter, Role.RECRUITER)

        with self.assertRaises(NotAllowed):
            self.upload(actor=recruiter, membership=membership)

    def test_a_member_granted_company_edit_can_attach(self):
        viewer = create_user()
        membership = add_member(
            self.company, viewer, Role.VIEWER, permissions=[Grant.COMPANY_EDIT.value]
        )
        self.upload(actor=viewer, membership=membership)
        self.assertEqual(self.company.verification.documents.count(), 1)

    def test_documents_are_frozen_while_under_review(self):
        document = self.upload()
        company_service.submit_verification(actor=self.owner, company=self.company)

        with self.assertRaises(InvalidState):
            self.upload()
        with self.assertRaises(InvalidState):
            company_service.remove_verification_document(
                actor=self.owner,
                membership=self.owner_membership,
                company=self.company,
                document_id=document.pk,
            )

    def test_more_information_reopens_the_documents(self):
        self.upload()
        company_service.submit_verification(actor=self.owner, company=self.company)
        staff = create_staff()
        company_service.claim_verification(actor=staff, company=self.company)
        company_service.review_verification(
            actor=staff,
            company=self.company,
            decision=VerificationState.MORE_INFO,
            reason="Add the PAN certificate.",
        )

        self.upload(kind="pan_vat")
        self.assertEqual(self.company.verification.documents.count(), 2)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(InvalidState):
            self.upload(kind="passport")

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(InvalidState):
            self.upload(uploaded_file=None)

    def test_bytes_that_are_not_a_document_are_refused(self):
        fake = file_factories.upload(b"MZ\x90\x00 not a pdf", "registration.pdf", "application/pdf")
        with self.assertRaises(DomainError):
            self.upload(uploaded_file=fake)
        self.assertEqual(self.company.verification.documents.count(), 0)

    def test_a_document_can_be_removed_before_submitting(self):
        document = self.upload()

        company_service.remove_verification_document(
            actor=self.owner,
            membership=self.owner_membership,
            company=self.company,
            document_id=document.pk,
        )

        self.assertEqual(self.company.verification.documents.count(), 0)
        document.attachment.refresh_from_db()
        self.assertIsNotNone(document.attachment.deleted_at)

    def test_another_company_s_document_is_not_found(self):
        other_owner = create_user()
        other = company_service.create_company(actor=other_owner, name="Other Co")
        other_membership = CompanyMembership.objects.get(company=other, user=other_owner)
        foreign = company_service.add_verification_document(
            actor=other_owner,
            membership=other_membership,
            company=other,
            kind="registration",
            uploaded_file=file_factories.pdf_upload(),
        )

        for bad_id in (foreign.pk, "not-a-number", None):
            with self.subTest(document_id=bad_id), self.assertRaises(NotFound):
                company_service.remove_verification_document(
                    actor=self.owner,
                    membership=self.owner_membership,
                    company=self.company,
                    document_id=bad_id,
                )


class WebsiteTests(TestCase):
    def test_a_website_that_is_not_an_address_is_refused(self):
        with self.assertRaises(InvalidState):
            company_service.create_company(
                actor=create_user(), name="Bad Site", website="not a url"
            )

    def test_a_bare_domain_gains_a_scheme(self):
        company = company_service.create_company(
            actor=create_user(), name="Bare Domain", website="bare.example.com"
        )
        self.assertEqual(company.website, "https://bare.example.com")

    def test_a_script_url_is_refused_on_edit(self):
        owner = create_user()
        company = company_service.create_company(actor=owner, name="Edit Site")
        with self.assertRaises(InvalidState):
            company_service.update_company(
                actor=owner,
                company=company,
                row_version=company.row_version,
                website="javascript:alert(1)",
            )


class InviteValidationTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner)

    def _invite(self, **kwargs):
        fields = {"email": "someone@example.com", "role": Role.RECRUITER, **kwargs}
        return membership_service.invite(
            actor=self.owner, actor_membership=self.owner_membership, company=self.company, **fields
        )

    def test_a_blank_or_malformed_email_is_refused(self):
        for email in ("", "   ", "not-an-address", "a@"):
            with self.subTest(email=email), self.assertRaises(InvalidState):
                self._invite(email=email)
        self.assertEqual(CompanyInvite.objects.count(), 0)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(InvalidState):
            self._invite(role="superuser")
        self.assertEqual(CompanyInvite.objects.count(), 0)


class RevokeInviteTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner)
        self.invite, self.token = membership_service.invite(
            actor=self.owner,
            actor_membership=self.owner_membership,
            company=self.company,
            email="invited@example.com",
            role=Role.RECRUITER,
        )

    def _revoke(self, membership=None, invite_id=None, actor=None):
        return membership_service.revoke_invite(
            actor=actor or self.owner,
            actor_membership=membership or self.owner_membership,
            invite_id=self.invite.pk if invite_id is None else invite_id,
        )

    def test_a_withdrawn_invitation_cannot_be_accepted(self):
        self._revoke()
        invitee = create_user(email="invited@example.com")

        with self.assertRaises(NotAllowed):
            membership_service.accept_invite(user=invitee, token=self.token)
        self.assertTrue(AuditLog.objects.filter(verb="team.invite_revoked").exists())

    def test_a_recruiter_cannot_withdraw(self):
        recruiter = create_user()
        membership = add_member(self.company, recruiter, Role.RECRUITER)
        with self.assertRaises(NotAllowed):
            self._revoke(membership=membership, actor=recruiter)

    def test_another_company_s_invitation_is_not_found(self):
        stranger = create_user()
        _, stranger_membership = company_with_owner(stranger)
        with self.assertRaises(NotFound):
            self._revoke(membership=stranger_membership, actor=stranger)
        self.invite.refresh_from_db()
        self.assertIsNone(self.invite.revoked_at)

    def test_a_garbage_id_is_not_found(self):
        with self.assertRaises(NotFound):
            self._revoke(invite_id="1 OR 1=1")

    def test_withdrawing_twice_is_refused(self):
        self._revoke()
        with self.assertRaises(InvalidState):
            self._revoke()

    def test_an_accepted_invitation_cannot_be_withdrawn(self):
        membership_service.accept_invite(
            user=create_user(email="invited@example.com"), token=self.token
        )
        with self.assertRaises(InvalidState):
            self._revoke()

    def test_an_admin_cannot_withdraw_an_owner_invitation(self):
        owner_invite, _ = membership_service.invite(
            actor=self.owner,
            actor_membership=self.owner_membership,
            company=self.company,
            email="future.owner@example.com",
            role=Role.OWNER,
        )
        admin = create_user()
        admin_membership = add_member(self.company, admin, Role.ADMIN)
        with self.assertRaises(NotAllowed):
            self._revoke(membership=admin_membership, actor=admin, invite_id=owner_invite.pk)


class ChangeRoleTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner)
        self.member = add_member(self.company, create_user(), Role.RECRUITER)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(InvalidState):
            membership_service.change_role(
                actor=self.owner,
                actor_membership=self.owner_membership,
                target=self.member,
                role="superuser",
            )
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, Role.RECRUITER)

    def test_a_member_removed_in_another_tab_is_not_found(self):
        # The view loaded the membership, then somebody removed it; the change
        # must decide on what is true now.
        stale = CompanyMembership.objects.get(pk=self.member.pk)
        CompanyMembership.objects.filter(pk=self.member.pk).update(deleted_at=timezone.now())

        with self.assertRaises(NotFound):
            membership_service.change_role(
                actor=self.owner,
                actor_membership=self.owner_membership,
                target=stale,
                role=Role.VIEWER,
            )

    def test_an_owner_who_was_demoted_meanwhile_cannot_create_an_owner(self):
        second_owner = add_member(self.company, create_user(), Role.OWNER)
        stale_actor = CompanyMembership.objects.get(pk=self.owner_membership.pk)
        CompanyMembership.objects.filter(pk=self.owner_membership.pk).update(role=Role.ADMIN)

        with self.assertRaises(NotAllowed):
            membership_service.change_role(
                actor=self.owner, actor_membership=stale_actor, target=self.member, role=Role.OWNER
            )
        self.assertEqual(second_owner.role, Role.OWNER)


class ReviewClaimTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company = company_service.create_company(actor=self.owner, name="Claim Co")
        membership = CompanyMembership.objects.get(company=self.company, user=self.owner)
        company_service.add_verification_document(
            actor=self.owner,
            membership=membership,
            company=self.company,
            kind="registration",
            uploaded_file=file_factories.pdf_upload(),
        )
        company_service.submit_verification(actor=self.owner, company=self.company)
        self.first = create_staff()
        self.second = create_staff()

    def _decide(self, actor, decision=VerificationState.VERIFIED, reason=""):
        return company_service.review_verification(
            actor=actor, company=self.company, decision=decision, reason=reason
        )

    def test_an_unclaimed_item_cannot_be_decided(self):
        with self.assertRaises(Conflict):
            self._decide(self.first)
        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_state, VerificationState.PENDING)

    def test_an_item_another_reviewer_holds_cannot_be_decided(self):
        company_service.claim_verification(actor=self.first, company=self.company)
        with self.assertRaises(Conflict):
            self._decide(self.second)

    def test_an_expired_claim_cannot_decide(self):
        company_service.claim_verification(actor=self.first, company=self.company)
        CompanyVerification.objects.filter(company=self.company).update(
            claimed_until=timezone.now() - dt.timedelta(minutes=1)
        )
        with self.assertRaises(Conflict):
            self._decide(self.first)

    def test_another_reviewer_s_live_claim_cannot_be_released(self):
        company_service.claim_verification(actor=self.first, company=self.company)
        with self.assertRaises(Conflict):
            company_service.release_verification(actor=self.second, company=self.company)

    def test_an_expired_claim_can_be_released_by_anyone(self):
        company_service.claim_verification(actor=self.first, company=self.company)
        CompanyVerification.objects.filter(company=self.company).update(
            claimed_until=timezone.now() - dt.timedelta(minutes=1)
        )
        verification = company_service.release_verification(actor=self.second, company=self.company)
        self.assertIsNone(verification.claimed_by)

    def test_a_decision_clears_the_claim(self):
        company_service.claim_verification(actor=self.first, company=self.company)
        verification = self._decide(self.first)
        self.assertIsNone(verification.claimed_by)
        self.assertIsNone(verification.claimed_until)

    def test_a_state_that_is_not_a_decision_is_refused(self):
        company_service.claim_verification(actor=self.first, company=self.company)
        for decision in (VerificationState.PENDING, VerificationState.NOT_SUBMITTED, "approved"):
            with self.subTest(decision=decision), self.assertRaises(InvalidState):
                self._decide(self.first, decision=decision)


class ReviewNotificationTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company = company_service.create_company(actor=self.owner, name="Notify Co")
        membership = CompanyMembership.objects.get(company=self.company, user=self.owner)
        self.admin = create_user()
        add_member(self.company, self.admin, Role.ADMIN)
        self.recruiter = create_user()
        add_member(self.company, self.recruiter, Role.RECRUITER)
        self.editor = create_user()
        add_member(self.company, self.editor, Role.VIEWER, permissions=[Grant.COMPANY_EDIT.value])
        self.removed = create_user()
        add_member(self.company, self.removed, Role.ADMIN, deleted_at=timezone.now())

        company_service.add_verification_document(
            actor=self.owner,
            membership=membership,
            company=self.company,
            kind="registration",
            uploaded_file=file_factories.pdf_upload(),
        )
        company_service.submit_verification(actor=self.owner, company=self.company)
        staff = create_staff()
        company_service.claim_verification(actor=staff, company=self.company)
        company_service.review_verification(
            actor=staff,
            company=self.company,
            decision=VerificationState.MORE_INFO,
            reason="The PAN certificate is missing.",
        )
        deliver()

    def test_the_people_who_can_act_are_told(self):
        rows = Notification.objects.filter(kind="company.verification_reviewed")
        self.assertEqual(
            set(rows.values_list("recipient_id", flat=True)),
            {self.owner.pk, self.admin.pk, self.editor.pk},
        )

    def test_the_notification_says_what_to_do_and_links_to_it(self):
        row = Notification.objects.get(kind="company.verification_reviewed", recipient=self.owner)
        self.assertEqual(row.title, "Notify Co needs more information")
        self.assertEqual(row.body, "The PAN certificate is missing.")
        self.assertEqual(row.target_url, reverse("employer:verification", args=[self.company.slug]))
