"""
Conversations and messages (SRS §12, ADR-A26).

The rule this file exists to defend is the first one: **a company cannot open a
conversation with a candidate it has no reason to see**. Everything else in
messaging is ordinary; that one is what separates a hiring inbox from a cold
outreach tool, and it is asserted from several directions.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.applications.models import GrantBasis
from apps.applications.services import pools as pool_service
from apps.applications.services import visibility
from apps.applications.tests.factories import seed_templates, submit
from apps.audit.models import AuditLog
from apps.candidates.models import BlockedCompany
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotAllowed, NotFound
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.messaging.models import (
    Conversation,
    ConversationReport,
    ConversationState,
    Message,
    ReportReason,
    ReportState,
    SenderKind,
)
from apps.messaging.services import conversations as service
from apps.notifications.models import OutboxEvent


class OpeningTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.profile = create_profile()

    def open(self, **kwargs):
        return service.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            **kwargs,
        )

    def test_a_company_with_no_reason_to_see_the_candidate_cannot_write(self):
        # INV-4. The check that makes cold outreach impossible rather than
        # discouraged — and NotFound, because a 403 would confirm the person
        # exists.
        with self.assertRaises(NotFound):
            self.open()
        self.assertEqual(Conversation.objects.count(), 0)

    def test_a_grant_is_what_makes_it_possible(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        conversation = self.open()
        self.assertEqual(conversation.company, self.company)

    def test_a_pooled_candidate_can_be_written_to(self):
        pool = pool_service.create_pool(company=self.company, actor=self.owner, name="Backend 2026")
        pool_service.add_member(pool=pool, candidate=self.profile, actor=self.owner)

        self.assertIsNotNone(self.open())

    def test_losing_the_grant_stops_new_threads(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        visibility.revoke_all(candidate=self.profile, company=self.company)

        with self.assertRaises(NotFound):
            self.open()

    def test_opening_twice_returns_the_same_thread(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        first = self.open()
        second = self.open()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Conversation.objects.count(), 1)

    def test_a_blocked_candidate_cannot_be_written_to(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        BlockedCompany.objects.create(profile=self.profile, company=self.company)

        with self.assertRaises(NotAllowed):
            self.open()

    def test_a_posting_from_another_company_cannot_anchor_the_thread(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        other, _ = company_with_owner(create_user(email="them@example.test"))
        their_job = publish(create_job(other))

        with self.assertRaises(NotFound):
            self.open(job=their_job)

    def test_opening_emits_an_event_and_writes_an_audit_row(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        self.open()

        self.assertTrue(OutboxEvent.objects.filter(type="conversation.opened").exists())
        self.assertTrue(AuditLog.objects.filter(verb="conversation.opened").exists())

    def test_an_archived_thread_reopens_rather_than_duplicating(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        conversation = self.open()
        service.archive(conversation=conversation, actor=self.owner)

        again = self.open()
        self.assertEqual(again.pk, conversation.pk)
        self.assertEqual(again.state, ConversationState.OPEN)

    def test_a_blocked_thread_does_not_reopen(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        conversation = self.open()
        service.block(conversation=conversation, actor=self.profile.user)

        # NotFound rather than NotAllowed, and that is the stronger answer: the
        # block revoked every grant, so the company no longer has any basis to
        # know this person exists.
        with self.assertRaises(NotFound):
            self.open()

        conversation.refresh_from_db()
        self.assertEqual(conversation.state, ConversationState.BLOCKED)

    def test_a_block_from_the_privacy_screen_closes_the_thread_too(self):
        # Same meaning from either entry point (SRS §12).
        from apps.candidates.services import blocking

        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        conversation = self.open()

        blocking.block_company(profile=self.profile, actor=self.profile.user, company=self.company)

        conversation.refresh_from_db()
        self.assertEqual(conversation.state, ConversationState.BLOCKED)
        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))


class SendTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        self.application = submit(job=self.job, profile=self.profile)
        self.conversation = service.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
            application=self.application,
        )

    def send(self, **kwargs):
        kwargs.setdefault("actor", self.owner)
        kwargs.setdefault("sender_kind", SenderKind.COMPANY)
        kwargs.setdefault("body", "Are you free on Thursday?")
        return service.send(conversation=self.conversation, **kwargs)

    def test_sending_stores_the_message_and_stamps_the_thread(self):
        message = self.send()

        self.conversation.refresh_from_db()
        self.assertEqual(message.body, "Are you free on Thursday?")
        self.assertEqual(self.conversation.last_message_at, message.created_at)

    def test_an_empty_message_is_refused(self):
        with self.assertRaises(InvalidState):
            self.send(body="   ")

    def test_an_invented_sender_kind_is_refused(self):
        with self.assertRaises(InvalidState):
            self.send(sender_kind="ghost")

    def test_the_sender_has_read_their_own_message(self):
        message = self.send()
        self.conversation.refresh_from_db()

        self.assertEqual(self.conversation.company_last_read_at, message.created_at)
        self.assertEqual(self.conversation.unread_for_company(), 0)

    def test_the_other_side_has_not(self):
        self.send()
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.unread_for_candidate(), 1)

    def test_reading_clears_the_count(self):
        self.send()
        service.mark_read(conversation=self.conversation, side=SenderKind.CANDIDATE)

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.unread_for_candidate(), 0)

    def test_the_candidate_can_reply(self):
        self.send()
        reply = self.send(
            actor=self.profile.user, sender_kind=SenderKind.CANDIDATE, body="Thursday works"
        )

        self.assertEqual(reply.sender_kind, SenderKind.CANDIDATE)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.unread_for_company(), 1)

    def test_sending_emits_the_event_that_the_preference_governs(self):
        self.send()
        self.assertTrue(OutboxEvent.objects.filter(type="message.sent").exists())

    def test_a_company_that_lost_its_grant_cannot_keep_writing(self):
        # A thread opened months ago is not a standing permission.
        visibility.revoke_all(candidate=self.profile, company=self.company)
        with self.assertRaises(NotFound):
            self.send()

    def test_a_blocked_thread_takes_no_more_messages(self):
        service.block(conversation=self.conversation, actor=self.profile.user)
        with self.assertRaises(NotAllowed):
            self.send()

    def test_a_candidate_can_still_reply_to_an_archived_thread_which_reopens_it(self):
        service.archive(conversation=self.conversation, actor=self.owner)
        self.send(actor=self.profile.user, sender_kind=SenderKind.CANDIDATE, body="Hello?")

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.state, ConversationState.OPEN)

    def test_too_many_attachments_are_refused(self):
        with self.assertRaises(InvalidState):
            self.send(attachments=[None] * (service.MAX_ATTACHMENTS + 1))


class RateLimitTests(TestCase):
    """SRS §12: two limits, because one is not enough."""

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
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

    def test_a_company_hits_the_per_candidate_limit(self):
        from apps.common.ratelimit import MESSAGE_PER_COMPANY_CANDIDATE, RateLimited

        for index in range(MESSAGE_PER_COMPANY_CANDIDATE.limit):
            service.send(
                conversation=self.conversation,
                actor=self.owner,
                sender_kind=SenderKind.COMPANY,
                body=f"Message {index}",
            )

        with self.assertRaises(RateLimited):
            service.send(
                conversation=self.conversation,
                actor=self.owner,
                sender_kind=SenderKind.COMPANY,
                body="One too many",
            )

    def test_the_candidate_is_not_rate_limited_in_their_own_thread(self):
        # The limits exist to stop bulk outreach. A candidate replying to a
        # company that wrote to them is not that.
        for index in range(30):
            service.send(
                conversation=self.conversation,
                actor=self.profile.user,
                sender_kind=SenderKind.CANDIDATE,
                body=f"Reply {index}",
            )
        self.assertEqual(Message.objects.count(), 30)


class MuteBlockReportTests(TestCase):
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

    def test_muting_one_side_leaves_the_other_alone(self):
        service.set_mute(conversation=self.conversation, side=SenderKind.CANDIDATE, muted=True)

        self.conversation.refresh_from_db()
        self.assertIsNotNone(self.conversation.candidate_muted_at)
        self.assertIsNone(self.conversation.company_muted_at)

    def test_muting_does_not_stop_messages_arriving(self):
        service.set_mute(conversation=self.conversation, side=SenderKind.CANDIDATE, muted=True)
        message = service.send(
            conversation=self.conversation,
            actor=self.owner,
            sender_kind=SenderKind.COMPANY,
            body="Still here",
        )
        # Muting silences notifications, not the thread.
        self.assertIsNotNone(message.pk)

    def test_unmuting_clears_it(self):
        service.set_mute(conversation=self.conversation, side=SenderKind.CANDIDATE, muted=True)
        service.set_mute(conversation=self.conversation, side=SenderKind.CANDIDATE, muted=False)

        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.candidate_muted_at)

    def test_an_invented_side_is_refused(self):
        with self.assertRaises(InvalidState):
            service.set_mute(conversation=self.conversation, side="referee", muted=True)

    def test_only_the_candidate_can_block(self):
        with self.assertRaises(NotAllowed):
            service.block(conversation=self.conversation, actor=self.owner)

    def test_blocking_ends_the_thread_and_hides_the_profile(self):
        service.block(conversation=self.conversation, actor=self.profile.user, reason="Spam")

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.state, ConversationState.BLOCKED)
        # All three effects. A block that left the profile readable would be a
        # setting, not a boundary.
        self.assertTrue(
            BlockedCompany.objects.filter(profile=self.profile, company=self.company).exists()
        )
        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_blocking_emits_an_event_and_an_audit_row(self):
        service.block(conversation=self.conversation, actor=self.profile.user)

        self.assertTrue(OutboxEvent.objects.filter(type="conversation.blocked").exists())
        self.assertTrue(AuditLog.objects.filter(verb="conversation.blocked").exists())

    def test_a_blocked_conversation_cannot_be_archived(self):
        service.block(conversation=self.conversation, actor=self.profile.user)
        with self.assertRaises(InvalidState):
            service.archive(conversation=self.conversation, actor=self.owner)

    def test_either_side_can_report(self):
        for actor in (self.profile.user, self.owner):
            with self.subTest(actor=actor):
                record = service.report(
                    conversation=self.conversation,
                    actor=actor,
                    reason=ReportReason.SPAM,
                    detail="Repeated messages",
                )
                self.assertEqual(record.state, ReportState.OPEN)

    def test_a_stranger_cannot_report_a_thread_they_are_not_in(self):
        with self.assertRaises(NotFound):
            service.report(
                conversation=self.conversation,
                actor=create_user(email="stranger@example.test"),
                reason=ReportReason.SPAM,
            )

    def test_an_invented_reason_is_refused(self):
        with self.assertRaises(InvalidState):
            service.report(
                conversation=self.conversation,
                actor=self.profile.user,
                reason="i just dont like them",
            )

    def test_reporting_emits_an_event_for_the_moderation_queue(self):
        service.report(
            conversation=self.conversation,
            actor=self.profile.user,
            reason=ReportReason.HARASSMENT,
        )
        self.assertTrue(OutboxEvent.objects.filter(type="conversation.reported").exists())

    def test_resolving_a_report_records_who_and_what(self):
        record = service.report(
            conversation=self.conversation,
            actor=self.profile.user,
            reason=ReportReason.SPAM,
        )
        staff = create_user(email="staff@example.test", is_staff=True)

        service.resolve_report(
            report_record=record,
            actor=staff,
            state=ReportState.UPHELD,
            resolution="Warned the company",
        )

        record.refresh_from_db()
        self.assertEqual(record.state, ReportState.UPHELD)
        self.assertEqual(record.reviewed_by, staff)
        self.assertIsNotNone(record.reviewed_at)

    def test_a_decision_needs_a_reason(self):
        record = service.report(
            conversation=self.conversation,
            actor=self.profile.user,
            reason=ReportReason.SPAM,
        )
        staff = create_user(email="staff@example.test", is_staff=True)

        with self.assertRaises(InvalidState):
            service.resolve_report(report_record=record, actor=staff, state=ReportState.DISMISSED)

    def test_a_dismissed_report_is_kept(self):
        # A company with three dismissed reports and a company with none are
        # different companies.
        record = service.report(
            conversation=self.conversation,
            actor=self.profile.user,
            reason=ReportReason.SPAM,
        )
        staff = create_user(email="staff@example.test", is_staff=True)
        service.resolve_report(
            report_record=record,
            actor=staff,
            state=ReportState.DISMISSED,
            resolution="No evidence",
        )

        self.assertEqual(ConversationReport.objects.count(), 1)


class EditDeleteTests(TestCase):
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
        self.message = service.send(
            conversation=self.conversation,
            actor=self.owner,
            sender_kind=SenderKind.COMPANY,
            body="Are you free thurdsay?",
        )

    def test_a_typo_can_be_fixed_inside_the_window(self):
        service.edit(message=self.message, actor=self.owner, body="Are you free Thursday?")

        self.message.refresh_from_db()
        self.assertEqual(self.message.body, "Are you free Thursday?")
        self.assertIsNotNone(self.message.edited_at)

    def test_nobody_else_can_edit_it(self):
        with self.assertRaises(NotAllowed):
            service.edit(message=self.message, actor=self.profile.user, body="Hijacked")

    def test_the_window_closes(self):
        Message.objects.filter(pk=self.message.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=2)
        )
        self.message.refresh_from_db()

        with self.assertRaises(InvalidState):
            service.edit(message=self.message, actor=self.owner, body="Too late")

    def test_deleting_leaves_a_tombstone(self):
        service.delete(message=self.message, actor=self.owner)

        self.message.refresh_from_db()
        self.assertTrue(self.message.is_deleted)
        self.assertEqual(self.message.display_body, "This message was deleted.")
        # The row survives: the other party already read it.
        self.assertTrue(Message.objects.filter(pk=self.message.pk).exists())

    def test_nobody_else_can_delete_it(self):
        with self.assertRaises(NotAllowed):
            service.delete(message=self.message, actor=self.profile.user)

    def test_a_deleted_message_cannot_be_edited(self):
        service.delete(message=self.message, actor=self.owner)
        with self.assertRaises(InvalidState):
            service.edit(message=self.message, actor=self.owner, body="Back again")
