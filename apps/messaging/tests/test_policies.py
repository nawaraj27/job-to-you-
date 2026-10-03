"""
Authorization for messaging (SRS §12), and what blocking means (SRS §5.6).

Two participants and no others. Every refusal across the boundary is NotFound,
because a 403 on a thread confirms that this company and this candidate are
talking — which is information neither of them agreed to publish.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.applications.services import visibility
from apps.applications.tests.factories import seed_templates, submit
from apps.audit.models import AuditLog
from apps.candidates.models import BlockedCompany
from apps.candidates.services import blocking
from apps.candidates.tests.factories import create_profile
from apps.common.errors import NotAllowed, NotFound
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.messaging import policies
from apps.messaging.models import ConversationState, SenderKind
from apps.messaging.services import conversations as service


class SideTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        submit(job=self.job, profile=self.profile)
        self.conversation = service.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
        )

    def test_the_company_side_is_readable_by_its_members(self):
        self.assertTrue(policies.can_read_as_company(self.conversation, self.membership))
        self.assertIs(
            policies.require_company_side(self.conversation, self.membership),
            self.conversation,
        )

    def test_another_companys_membership_is_refused(self):
        _, theirs = company_with_owner(create_user(email="them@example.test"))
        self.assertFalse(policies.can_read_as_company(self.conversation, theirs))
        with self.assertRaises(NotFound):
            policies.require_company_side(self.conversation, theirs)

    def test_no_membership_is_refused(self):
        self.assertFalse(policies.can_read_as_company(self.conversation, None))
        with self.assertRaises(NotFound):
            policies.require_company_side(self.conversation, None)

    def test_a_suspended_member_is_refused(self):
        self.membership.status = self.membership.Status.SUSPENDED
        self.membership.save(update_fields=["status"])
        self.assertFalse(policies.can_read_as_company(self.conversation, self.membership))

    def test_the_candidate_side_is_readable_by_its_owner(self):
        self.assertIs(
            policies.require_candidate_side(self.conversation, self.profile),
            self.conversation,
        )

    def test_another_candidate_is_refused(self):
        with self.assertRaises(NotFound):
            policies.require_candidate_side(self.conversation, create_profile())

    def test_an_anonymous_reader_is_refused(self):
        with self.assertRaises(NotFound):
            policies.require_candidate_side(self.conversation, None)

    def test_side_for_names_the_side(self):
        self.assertEqual(policies.side_for(self.conversation, self.owner), SenderKind.COMPANY)
        self.assertEqual(
            policies.side_for(self.conversation, self.profile.user), SenderKind.CANDIDATE
        )

    def test_side_for_refuses_a_stranger(self):
        with self.assertRaises(NotFound):
            policies.side_for(self.conversation, create_user(email="nobody@example.test"))

    def test_an_open_thread_passes_the_open_check(self):
        self.assertIs(policies.require_open(self.conversation), self.conversation)

    def test_a_blocked_thread_fails_it(self):
        service.block(conversation=self.conversation, actor=self.profile.user)
        self.conversation.refresh_from_db()

        with self.assertRaises(NotAllowed):
            policies.require_open(self.conversation)


class BlockingTests(TestCase):
    """What a block means, from either entry point (SRS §5.6, §12)."""

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        submit(job=self.job, profile=self.profile)

    def test_blocking_writes_the_row_and_revokes_access(self):
        blocking.block_company(
            profile=self.profile, actor=self.profile.user, company=self.company, reason="Spam"
        )

        self.assertTrue(
            BlockedCompany.objects.filter(profile=self.profile, company=self.company).exists()
        )
        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_blocking_closes_an_open_thread(self):
        conversation = service.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
        )

        blocking.block_company(profile=self.profile, actor=self.profile.user, company=self.company)

        conversation.refresh_from_db()
        self.assertEqual(conversation.state, ConversationState.BLOCKED)

    def test_blocking_twice_is_idempotent(self):
        for _ in range(2):
            blocking.block_company(
                profile=self.profile, actor=self.profile.user, company=self.company
            )
        self.assertEqual(BlockedCompany.objects.count(), 1)

    def test_blocking_is_audited(self):
        blocking.block_company(profile=self.profile, actor=self.profile.user, company=self.company)
        self.assertTrue(AuditLog.objects.filter(verb="company.blocked").exists())

    def test_unblocking_removes_the_row(self):
        blocking.block_company(profile=self.profile, actor=self.profile.user, company=self.company)
        removed = blocking.unblock_company(
            profile=self.profile, actor=self.profile.user, company=self.company
        )

        self.assertEqual(removed, 1)
        self.assertFalse(BlockedCompany.objects.exists())

    def test_unblocking_does_not_restore_the_revoked_access(self):
        # Deliberately asymmetrical. Reviving a revoked grant would resurrect
        # access the candidate had already withdrawn.
        blocking.block_company(profile=self.profile, actor=self.profile.user, company=self.company)
        blocking.unblock_company(
            profile=self.profile, actor=self.profile.user, company=self.company
        )

        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_unblocking_somebody_who_was_never_blocked_is_quiet(self):
        self.assertEqual(
            blocking.unblock_company(
                profile=self.profile, actor=self.profile.user, company=self.company
            ),
            0,
        )

    def test_unblocking_is_audited(self):
        blocking.block_company(profile=self.profile, actor=self.profile.user, company=self.company)
        blocking.unblock_company(
            profile=self.profile, actor=self.profile.user, company=self.company
        )
        self.assertTrue(AuditLog.objects.filter(verb="company.unblocked").exists())

    def test_a_blocked_company_cannot_be_applied_to_again(self):
        from apps.applications.services import apply as apply_service
        from apps.applications.tests.factories import create_version
        from apps.common.errors import InvalidState

        blocking.block_company(profile=self.profile, actor=self.profile.user, company=self.company)
        other_job = publish(create_job(self.company))

        with self.assertRaises(InvalidState):
            apply_service.apply_to_job(
                job=other_job,
                profile=self.profile,
                actor=self.profile.user,
                resume_version=create_version(self.profile),
            )


class AttachmentSharingTests(TestCase):
    """ADR-A52, end to end: a message attachment reaches its recipient."""

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        submit(job=self.job, profile=self.profile)
        self.conversation = service.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
        )

    def _attachment(self):
        from apps.files.constants import ScanStatus, Visibility
        from apps.files.models import Attachment

        return Attachment.objects.create(
            storage_key="ab/cd/brief.pdf",
            filename="brief.pdf",
            mime="application/pdf",
            size=2048,
            sha256="c" * 64,
            visibility=Visibility.PRIVATE,
            scan_status=ScanStatus.CLEAN,
            uploaded_by=self.owner,
        )

    def test_a_file_sent_by_the_company_becomes_readable_by_the_candidate(self):
        from apps.files import policies as file_policies

        attachment = self._attachment()
        self.assertFalse(file_policies.can_access(self.profile.user, attachment))

        service.send(
            conversation=self.conversation,
            actor=self.owner,
            sender_kind=SenderKind.COMPANY,
            body="The brief is attached.",
            attachments=[attachment],
        )

        self.assertTrue(file_policies.can_access(self.profile.user, attachment))

    def test_a_stranger_still_cannot_read_it(self):
        from apps.files import policies as file_policies

        attachment = self._attachment()
        service.send(
            conversation=self.conversation,
            actor=self.owner,
            sender_kind=SenderKind.COMPANY,
            body="The brief is attached.",
            attachments=[attachment],
        )

        stranger = create_user(email="stranger@example.test")
        self.assertFalse(file_policies.can_access(stranger, attachment))

    def test_deleting_the_message_takes_the_access_back(self):
        from apps.files import policies as file_policies

        attachment = self._attachment()
        message = service.send(
            conversation=self.conversation,
            actor=self.owner,
            sender_kind=SenderKind.COMPANY,
            body="The brief is attached.",
            attachments=[attachment],
        )

        service.delete(message=message, actor=self.owner)

        self.assertFalse(file_policies.can_access(self.profile.user, attachment))
