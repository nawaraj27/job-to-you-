"""
Scheduling (FR-10.6, FR-10.7, SRS §11.3).

The property this file exists for: **double-booking is impossible**, and it is
impossible because Postgres refuses it rather than because the application
checked first. `test_the_database_refuses_an_overlap_whatever_the_service_does`
writes directly through the ORM, bypassing every service guard, and asserts the
constraint still holds — which is the only way to test a database guarantee.

The rest is the traffic that actually happens: holds that expire, reschedules
that keep the thread, cancellations that free the slot, and no-shows that
record which side did not come.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.candidates.tests.factories import create_profile
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.companies.models import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.interviews.models import (
    InterviewParticipant,
    InterviewSession,
    InterviewSlot,
    SessionState,
    SlotState,
)
from apps.interviews.services import scheduling
from apps.interviews.tests.factories import live_template, window
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for
from apps.notifications.models import OutboxEvent


class SchedulingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))

        self.candidate = create_profile()
        self.application = application_for(self.candidate, self.job)
        self.template = live_template()

    def publish_one(self, *, hours_ahead: int = 24) -> InterviewSlot:
        return scheduling.publish_slots(
            membership=self.membership,
            actor=self.owner,
            windows=[window(hours_ahead=hours_ahead)],
        )[0]

    def book_one(self, slot: InterviewSlot | None = None) -> InterviewSession:
        return scheduling.book(
            slot=slot or self.publish_one(),
            application=self.application,
            actor=self.owner,
            template=self.template,
        )


class PublishTests(SchedulingFixture):
    def test_a_window_becomes_an_open_slot(self):
        slot = self.publish_one()
        self.assertEqual(slot.state, SlotState.OPEN)
        self.assertTrue(slot.is_bookable)

    def test_a_backwards_window_is_refused(self):
        starts = timezone.now() + dt.timedelta(hours=2)
        with self.assertRaises(InvalidState):
            scheduling.publish_slots(
                membership=self.membership,
                actor=self.owner,
                windows=[(starts, starts - dt.timedelta(minutes=30))],
            )

    def test_a_window_in_the_past_is_refused(self):
        starts = timezone.now() - dt.timedelta(hours=2)
        with self.assertRaises(InvalidState):
            scheduling.publish_slots(
                membership=self.membership,
                actor=self.owner,
                windows=[(starts, starts + dt.timedelta(minutes=30))],
            )

    def test_a_window_beyond_the_horizon_is_refused(self):
        with self.assertRaises(InvalidState):
            scheduling.publish_slots(
                membership=self.membership,
                actor=self.owner,
                windows=[window(hours_ahead=24 * (scheduling.MAX_HORIZON_DAYS + 5))],
            )

    def test_publishing_two_overlapping_windows_is_refused(self):
        # The same constraint that refuses a double booking. An interviewer
        # publishing overlapping windows is the same mistake.
        starts, ends = window(hours_ahead=48)
        scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[(starts, ends)]
        )

        with self.assertRaises(Conflict):
            scheduling.publish_slots(
                membership=self.membership,
                actor=self.owner,
                windows=[(starts + dt.timedelta(minutes=10), ends + dt.timedelta(minutes=10))],
            )

    def test_two_interviewers_may_hold_the_same_window(self):
        # The constraint is per interviewer. Two people interviewing at once is
        # a company doing its job.
        other = create_profile().user
        other_membership = add_member(self.company, other, Role.INTERVIEWER)
        starts, ends = window(hours_ahead=72)

        scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[(starts, ends)]
        )
        created = scheduling.publish_slots(
            membership=other_membership, actor=other, windows=[(starts, ends)]
        )
        self.assertEqual(len(created), 1)

    def test_publishing_is_audited(self):
        from apps.audit.models import AuditLog

        self.publish_one()
        self.assertTrue(AuditLog.objects.filter(verb="interview.slots_published").exists())


class HoldTests(SchedulingFixture):
    def test_a_hold_takes_a_slot_off_the_market(self):
        slot = self.publish_one()
        held = scheduling.hold(slot=slot, actor=self.candidate.user)

        self.assertEqual(held.state, SlotState.HELD)
        self.assertTrue(held.is_held)
        self.assertNotIn(held, scheduling.open_slots(company=self.company))

    def test_a_second_hold_on_a_live_hold_is_refused(self):
        slot = self.publish_one()
        scheduling.hold(slot=slot, actor=self.candidate.user)

        with self.assertRaises(Conflict):
            scheduling.hold(slot=slot, actor=create_profile().user)

    def test_an_expired_hold_is_bookable_again(self):
        # A slot lost to somebody who closed a tab is a slot lost.
        slot = self.publish_one()
        scheduling.hold(slot=slot, actor=self.candidate.user)

        InterviewSlot.objects.filter(pk=slot.pk).update(
            held_until=timezone.now() - dt.timedelta(minutes=1)
        )
        slot.refresh_from_db()

        self.assertTrue(slot.is_bookable)
        self.assertIn(slot, scheduling.open_slots(company=self.company))

    def test_the_sweep_returns_expired_holds_to_open(self):
        slot = self.publish_one()
        scheduling.hold(slot=slot, actor=self.candidate.user)
        InterviewSlot.objects.filter(pk=slot.pk).update(
            held_until=timezone.now() - dt.timedelta(minutes=1)
        )

        self.assertEqual(scheduling.sweep_holds(), 1)
        slot.refresh_from_db()
        self.assertEqual(slot.state, SlotState.OPEN)

    def test_the_sweep_leaves_a_live_hold_alone(self):
        slot = self.publish_one()
        scheduling.hold(slot=slot, actor=self.candidate.user)

        self.assertEqual(scheduling.sweep_holds(), 0)


class BookingTests(SchedulingFixture):
    def test_booking_creates_a_scheduled_session(self):
        session = self.book_one()

        self.assertEqual(session.state, SessionState.SCHEDULED)
        self.assertEqual(session.kind, "live")
        self.assertIsNotNone(session.scheduled_at)

    def test_booking_reserves_a_private_room(self):
        session = self.book_one()

        self.assertTrue(session.room_ref)
        self.assertTrue(session.join_token_hash)
        # The room reference carries nothing about who is inside: a guessable
        # room name is a stranger walking into an interview (§11.3).
        self.assertNotIn(self.job.title.lower(), session.room_ref.lower())

    def test_the_join_token_is_stored_hashed(self):
        # A join token is a credential, and a stored credential is a leak
        # waiting for a backup to be read.
        session = self.book_one()
        self.assertEqual(len(session.join_token_hash), 64)

    def test_booking_marks_the_slot_and_links_the_session(self):
        slot = self.publish_one()
        session = self.book_one(slot)
        slot.refresh_from_db()

        self.assertEqual(slot.state, SlotState.BOOKED)
        self.assertEqual(slot.session_id, session.pk)
        self.assertIsNone(slot.held_until)

    def test_booking_invites_the_interviewer(self):
        session = self.book_one()
        self.assertTrue(
            InterviewParticipant.objects.filter(session=session, user=self.owner).exists()
        )

    def test_booking_a_booked_slot_is_refused(self):
        slot = self.publish_one()
        self.book_one(slot)
        second = application_for(create_profile(), publish(create_job(self.company)))

        with self.assertRaises(Conflict):
            scheduling.book(slot=slot, application=second, actor=self.owner, template=self.template)

    def test_booking_a_cancelled_slot_is_not_found(self):
        slot = self.publish_one()
        InterviewSlot.objects.filter(pk=slot.pk).update(state=SlotState.CANCELLED)

        with self.assertRaises(NotFound):
            scheduling.book(
                slot=slot,
                application=self.application,
                actor=self.owner,
                template=self.template,
            )

    def test_booking_emits_an_event_for_both_sides(self):
        session = self.book_one()
        event = OutboxEvent.objects.filter(type="interview.scheduled").first()

        self.assertIsNotNone(event)
        self.assertEqual(event.payload["session_public_id"], str(session.public_id))

    def test_booking_is_audited(self):
        from apps.audit.models import AuditLog

        self.book_one()
        self.assertTrue(AuditLog.objects.filter(verb="interview.scheduled").exists())


class OverlapTests(TransactionTestCase):
    """The database guarantee (FR-10.6).

    `TransactionTestCase` because an `IntegrityError` inside the wrapping
    transaction of a `TestCase` breaks it for everything afterwards — and
    because what is being tested is a constraint the database enforces at
    commit, which is not something a rolled-back test can observe honestly.
    """

    reset_sequences = True

    def setUp(self):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)

    def test_the_database_refuses_an_overlap_whatever_the_service_does(self):
        """The only test that proves the constraint rather than the guard.

        Written directly through the ORM, bypassing `publish_slots` entirely.
        A check-then-insert in a service would pass this test's first half and
        fail its second; only the exclusion constraint refuses both.
        """
        starts, ends = window(hours_ahead=36)
        InterviewSlot.objects.create(interviewer=self.membership, starts_at=starts, ends_at=ends)

        with self.assertRaises(IntegrityError), transaction.atomic():
            InterviewSlot.objects.create(
                interviewer=self.membership,
                starts_at=starts + dt.timedelta(minutes=5),
                ends_at=ends + dt.timedelta(minutes=5),
            )

    def test_a_cancelled_slot_frees_its_window(self):
        # `WHERE state <> 'cancelled'` on the constraint. Without it, a
        # cancelled slot would block its window forever and an interviewer's
        # calendar would fill up with times nobody can use.
        starts, ends = window(hours_ahead=40)
        first = InterviewSlot.objects.create(
            interviewer=self.membership, starts_at=starts, ends_at=ends
        )
        InterviewSlot.objects.filter(pk=first.pk).update(state=SlotState.CANCELLED)

        second = InterviewSlot.objects.create(
            interviewer=self.membership, starts_at=starts, ends_at=ends
        )
        self.assertEqual(second.state, SlotState.OPEN)

    def test_adjacent_windows_do_not_overlap(self):
        # A slot ending at 10:00 and one starting at 10:00 are back to back,
        # not overlapping — `tstzrange` is half-open, which is the behaviour a
        # calendar needs.
        starts, ends = window(hours_ahead=44, minutes=30)
        InterviewSlot.objects.create(interviewer=self.membership, starts_at=starts, ends_at=ends)
        adjacent = InterviewSlot.objects.create(
            interviewer=self.membership,
            starts_at=ends,
            ends_at=ends + dt.timedelta(minutes=30),
        )
        self.assertIsNotNone(adjacent.pk)


class RescheduleTests(SchedulingFixture):
    def test_rescheduling_creates_a_linked_session(self):
        # Both sides need to see continuity rather than a cancellation followed
        # by a surprise.
        original = self.book_one()
        later = self.publish_one(hours_ahead=96)

        moved = scheduling.reschedule(session=original, slot=later, actor=self.owner)

        self.assertEqual(moved.rescheduled_from_id, original.pk)
        self.assertEqual(moved.state, SessionState.SCHEDULED)

    def test_rescheduling_cancels_the_original(self):
        original = self.book_one()
        later = self.publish_one(hours_ahead=96)
        scheduling.reschedule(session=original, slot=later, actor=self.owner)

        original.refresh_from_db()
        self.assertEqual(original.state, SessionState.CANCELLED)

    def test_rescheduling_frees_the_original_slot(self):
        slot = self.publish_one()
        original = self.book_one(slot)
        later = self.publish_one(hours_ahead=96)

        scheduling.reschedule(session=original, slot=later, actor=self.owner)

        slot.refresh_from_db()
        self.assertEqual(slot.state, SlotState.OPEN)
        self.assertIsNone(slot.session_id)

    def test_rescheduling_emits_its_own_event(self):
        original = self.book_one()
        later = self.publish_one(hours_ahead=96)
        scheduling.reschedule(session=original, slot=later, actor=self.owner)

        self.assertTrue(OutboxEvent.objects.filter(type="interview.rescheduled").exists())

    def test_a_finished_interview_cannot_be_rescheduled(self):
        session = self.book_one()
        scheduling.cancel(session=session, actor=self.owner)
        later = self.publish_one(hours_ahead=96)

        with self.assertRaises(InvalidState):
            scheduling.reschedule(session=session, slot=later, actor=self.owner)

    def test_a_practice_session_cannot_be_rescheduled(self):
        practice = InterviewSession.objects.create(
            candidate=self.candidate, template=self.template, kind="ai_text"
        )
        later = self.publish_one(hours_ahead=96)

        with self.assertRaises(InvalidState):
            scheduling.reschedule(session=practice, slot=later, actor=self.owner)


class CancelTests(SchedulingFixture):
    def test_cancelling_frees_the_slot(self):
        # An interviewer whose cancelled interviews silently consumed their
        # calendar would stop publishing availability within a week.
        slot = self.publish_one()
        session = self.book_one(slot)

        scheduling.cancel(session=session, actor=self.owner, reason="Role on hold")

        slot.refresh_from_db()
        self.assertEqual(slot.state, SlotState.OPEN)
        self.assertIn(slot, scheduling.open_slots(company=self.company))

    def test_cancelling_records_the_reason(self):
        session = self.book_one()
        scheduling.cancel(session=session, actor=self.owner, reason="Role on hold")

        session.refresh_from_db()
        self.assertEqual(session.state, SessionState.CANCELLED)
        self.assertEqual(session.cancelled_reason, "Role on hold")

    def test_cancelling_emits_an_event(self):
        session = self.book_one()
        scheduling.cancel(session=session, actor=self.owner)
        self.assertTrue(OutboxEvent.objects.filter(type="interview.cancelled").exists())

    def test_cancelling_twice_is_refused(self):
        session = self.book_one()
        scheduling.cancel(session=session, actor=self.owner)

        with self.assertRaises(InvalidState):
            scheduling.cancel(session=session, actor=self.owner)


class NoShowTests(SchedulingFixture):
    def arrived(self, session):
        """A no-show is only recordable once the interview time has come."""
        session.scheduled_at = timezone.now() - dt.timedelta(minutes=10)
        session.save(update_fields=["scheduled_at", "updated_at"])
        return session

    def test_a_no_show_before_the_interview_time_is_refused(self):
        session = self.book_one()
        with self.assertRaises(InvalidState):
            scheduling.mark_no_show(session=session, actor=self.owner)

    def test_a_no_show_records_which_side_did_not_come(self):
        # A candidate no-show and an interviewer no-show are different failures
        # with different apologies owed.
        session = self.arrived(self.book_one())
        scheduling.mark_no_show(session=session, actor=self.owner, missing_side="interviewer")

        session.refresh_from_db()
        self.assertEqual(session.state, SessionState.NO_SHOW)
        self.assertIn("interviewer", session.cancelled_reason)

    def test_the_event_carries_the_missing_side(self):
        session = self.arrived(self.book_one())
        scheduling.mark_no_show(session=session, actor=self.owner, missing_side="candidate")

        event = OutboxEvent.objects.filter(type="interview.no_show").first()
        self.assertEqual(event.payload["missing_side"], "candidate")

    def test_an_unnamed_side_is_refused(self):
        session = self.book_one()
        with self.assertRaises(InvalidState):
            scheduling.mark_no_show(session=session, actor=self.owner, missing_side="somebody")

    def test_a_no_show_frees_the_slot(self):
        slot = self.publish_one()
        session = self.arrived(self.book_one(slot))
        scheduling.mark_no_show(session=session, actor=self.owner)

        slot.refresh_from_db()
        self.assertEqual(slot.state, SlotState.OPEN)


class UpcomingTests(SchedulingFixture):
    def test_a_candidate_sees_their_own_upcoming_interview(self):
        session = self.book_one()
        upcoming = scheduling.upcoming_for(candidate=self.candidate)
        self.assertIn(session, upcoming)

    def test_a_candidate_does_not_see_somebody_elses(self):
        self.book_one()
        stranger = create_profile()
        self.assertEqual(list(scheduling.upcoming_for(candidate=stranger)), [])

    def test_an_interviewer_sees_what_they_are_in(self):
        session = self.book_one()
        upcoming = scheduling.upcoming_for(membership=self.membership)
        self.assertIn(session, upcoming)

    def test_a_cancelled_interview_is_not_upcoming(self):
        session = self.book_one()
        scheduling.cancel(session=session, actor=self.owner)
        self.assertEqual(list(scheduling.upcoming_for(candidate=self.candidate)), [])
