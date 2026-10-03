"""
Background work, the pipeline subscriber and the access window
(FR-10.6, FR-10.7, §11.4, ADR-A9).

Two things worth stating about what is asserted here.

**A completed interview does not move an application.** The consumer records the
event on the timeline and stops. Moving somebody to rejected because a model
scored them 2.4 would be exactly the automated adjudication ADR-A9 refuses, and
moving them forward is the same mistake wearing a friendlier face.

**Reminders are idempotent through the delivery ledger, not through this task.**
The sweep may run twice; the ledger's unique constraint refuses the duplicate
(ADR-A25), which is why nothing here tracks what it has already sent.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.applications.models import ApplicationEvent
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.interviews import policies
from apps.interviews.models import InterviewSession, InterviewTemplate, SessionState
from apps.interviews.services import engine, scheduling
from apps.interviews.tasks import (
    close_stale_sessions,
    evaluate_session,
    send_reminders,
    sweep_holds,
)
from apps.interviews.tests.factories import (
    EVALUATION_OUTPUT,
    fake_ai,
    live_template,
    practice_template,
    window,
)
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for
from apps.notifications.models import OutboxEvent


class TaskFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        self.profile = create_profile()
        self.template = practice_template()
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))


class EvaluateTaskTests(TaskFixture):
    def finished(self) -> InterviewSession:
        with fake_ai():
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )
            turn = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=turn, text="I cut it from four hours.")
        session.state = SessionState.COMPLETED
        session.save(update_fields=["state", "updated_at"])
        FakeAdapter.reset()
        return session

    def test_the_task_scores_a_finished_session(self):
        session = self.finished()
        with fake_ai():
            FakeAdapter.queue(*[EVALUATION_OUTPUT] * 4)
            result = evaluate_session(session.pk)

        self.assertTrue(result.startswith("scored:"))
        self.assertTrue(session.evaluations.exists())

    def test_a_deleted_session_is_not_an_error(self):
        self.assertEqual(evaluate_session(987654), "gone")

    def test_an_unfinished_session_is_refused_rather_than_retried(self):
        # A retry storm against a session that will never be scorable spends
        # the company's budget to reach the same place.
        with fake_ai():
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )
        self.assertEqual(evaluate_session(session.pk), "refused")

    def test_an_unavailable_model_reports_rather_than_raises(self):
        session = self.finished()
        with fake_ai():
            self.assertEqual(evaluate_session(session.pk), "unavailable")


class ReminderTests(TaskFixture):
    def book(self, *, hours_ahead: int) -> InterviewSession:
        slot = scheduling.publish_slots(
            membership=self.membership,
            actor=self.owner,
            windows=[window(hours_ahead=hours_ahead)],
        )[0]
        return scheduling.book(
            slot=slot,
            application=application_for(create_profile(), self.job),
            actor=self.owner,
            template=live_template(slug=f"live-{hours_ahead}"),
        )

    def test_an_interview_a_day_away_is_reminded(self):
        self.book(hours_ahead=24)
        OutboxEvent.objects.all().delete()

        self.assertEqual(send_reminders()["reminded"], 1)
        event = OutboxEvent.objects.filter(type="interview.scheduled").first()
        self.assertEqual(event.payload["reminder"], "24h")

    def test_an_interview_an_hour_away_is_reminded(self):
        self.book(hours_ahead=1)
        OutboxEvent.objects.all().delete()

        self.assertEqual(send_reminders()["reminded"], 1)
        self.assertEqual(
            OutboxEvent.objects.filter(type="interview.scheduled").first().payload["reminder"],
            "1h",
        )

    def test_an_interview_next_week_is_not_reminded_yet(self):
        self.book(hours_ahead=24 * 7)
        OutboxEvent.objects.all().delete()

        self.assertEqual(send_reminders()["reminded"], 0)

    def test_a_cancelled_interview_is_not_reminded(self):
        session = self.book(hours_ahead=24)
        scheduling.cancel(session=session, actor=self.owner)
        OutboxEvent.objects.all().delete()

        self.assertEqual(send_reminders()["reminded"], 0)


class SweepTests(TaskFixture):
    def test_the_hold_sweep_returns_expired_holds(self):
        from apps.interviews.models import InterviewSlot, SlotState

        slot = scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[window(hours_ahead=20)]
        )[0]
        scheduling.hold(slot=slot, actor=self.profile.user)
        InterviewSlot.objects.filter(pk=slot.pk).update(
            held_until=timezone.now() - dt.timedelta(minutes=1)
        )

        self.assertEqual(sweep_holds(), 1)
        slot.refresh_from_db()
        self.assertEqual(slot.state, SlotState.OPEN)

    def test_a_session_left_open_is_abandoned(self):
        # A row that blocks the candidate from starting another and tells the
        # employer nothing.
        with fake_ai():
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )
        InterviewSession.objects.filter(pk=session.pk).update(
            started_at=timezone.now() - dt.timedelta(hours=12)
        )

        self.assertEqual(close_stale_sessions(), 1)
        session.refresh_from_db()
        self.assertEqual(session.state, SessionState.ABANDONED)

    def test_a_session_started_recently_is_left_alone(self):
        with fake_ai():
            engine.start(candidate=self.profile, template=self.template, actor=self.profile.user)
        self.assertEqual(close_stale_sessions(), 0)


class PipelineConsumerTests(TaskFixture):
    def test_a_completed_interview_lands_on_the_application_timeline(self):
        from apps.interviews.consumers import record_on_timeline

        application = application_for(self.profile, self.job)
        session = InterviewSession.objects.create(
            application=application,
            candidate=self.profile,
            template=self.template,
            kind="ai_text",
            state=SessionState.COMPLETED,
        )
        record_on_timeline(
            OutboxEvent(
                type="interview.completed",
                payload={"session_public_id": str(session.public_id)},
            )
        )

        self.assertTrue(
            ApplicationEvent.objects.filter(
                application=application, type="interview.completed"
            ).exists()
        )

    def test_a_completed_interview_does_not_move_the_application(self):
        # ADR-A9. An interview recommends; a person decides.
        from apps.interviews.consumers import record_on_timeline

        application = application_for(self.profile, self.job)
        before = application.stage_id
        session = InterviewSession.objects.create(
            application=application,
            candidate=self.profile,
            template=self.template,
            kind="ai_text",
            state=SessionState.COMPLETED,
        )
        record_on_timeline(
            OutboxEvent(
                type="interview.completed",
                payload={"session_public_id": str(session.public_id)},
            )
        )

        application.refresh_from_db()
        self.assertEqual(application.stage_id, before)

    def test_a_practice_session_lands_on_nobodys_timeline(self):
        from apps.interviews.consumers import record_on_timeline

        with fake_ai():
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )
        record_on_timeline(
            OutboxEvent(
                type="interview.completed",
                payload={"session_public_id": str(session.public_id)},
            )
        )
        self.assertFalse(ApplicationEvent.objects.filter(type="interview.completed").exists())

    def test_an_event_naming_nothing_is_ignored(self):
        from apps.interviews.consumers import record_on_timeline

        record_on_timeline(OutboxEvent(type="interview.completed", payload={}))
        self.assertFalse(ApplicationEvent.objects.filter(type="interview.completed").exists())


def _panel_interviewer(company, session):
    """An Interviewer member on this session's panel: the role the thirty-day
    window belongs to (SRS §2.3)."""
    from apps.accounts.tests.factories import create_user
    from apps.companies.permissions import Role
    from apps.companies.tests.factories import add_member
    from apps.interviews.models import InterviewParticipant

    membership = add_member(company, create_user(), Role.INTERVIEWER)
    InterviewParticipant.objects.create(session=session, user=membership.user)
    return membership


class PolicyTests(TaskFixture):
    def setUp(self):
        super().setUp()
        self.application = application_for(self.profile, self.job)
        self.session = InterviewSession.objects.create(
            application=self.application,
            candidate=self.profile,
            template=self.template,
            kind="live",
            state=SessionState.SCHEDULED,
            scheduled_at=timezone.now(),
        )

    def test_the_candidate_may_read_their_own_session(self):
        self.assertEqual(policies.require_candidate(self.session, self.profile), self.session)

    def test_another_candidate_is_not_found(self):
        from apps.common.errors import NotFound

        with self.assertRaises(NotFound):
            policies.require_candidate(self.session, create_profile())

    def test_an_anonymous_reader_is_not_found(self):
        from apps.common.errors import NotFound

        with self.assertRaises(NotFound):
            policies.require_candidate(self.session, None)

    def test_the_employer_may_read_it_within_the_window(self):
        self.assertEqual(policies.require_employer(self.session, self.membership), self.session)

    def test_an_interviewer_cannot_read_it_after_the_window(self):
        from apps.common.errors import NotFound

        interviewer = _panel_interviewer(self.company, self.session)
        InterviewSession.objects.filter(pk=self.session.pk).update(
            ended_at=timezone.now() - dt.timedelta(days=policies.ACCESS_DAYS_AFTER + 5)
        )
        self.session.refresh_from_db()

        with self.assertRaises(NotFound):
            policies.require_employer(self.session, interviewer)

    def test_the_owner_keeps_the_record_after_the_window(self):
        # The thirty days are the Interviewer's (SRS §2.3). Applied to everybody,
        # a company lost the scorecards behind a hire a month later.
        InterviewSession.objects.filter(pk=self.session.pk).update(
            ended_at=timezone.now() - dt.timedelta(days=policies.ACCESS_DAYS_AFTER + 5)
        )
        self.session.refresh_from_db()

        self.assertEqual(policies.require_employer(self.session, self.membership), self.session)

    def test_an_employer_cannot_read_a_practice_session(self):
        from apps.common.errors import NotFound

        with fake_ai():
            practice = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )
        with self.assertRaises(NotFound):
            policies.require_employer(practice, self.membership)

    def test_the_room_opens_around_the_scheduled_time(self):
        self.assertTrue(policies.can_join(self.session))

    def test_the_room_is_shut_long_before(self):
        InterviewSession.objects.filter(pk=self.session.pk).update(
            scheduled_at=timezone.now() + dt.timedelta(hours=3)
        )
        self.session.refresh_from_db()
        self.assertFalse(policies.can_join(self.session))

    def test_the_room_is_shut_long_after(self):
        InterviewSession.objects.filter(pk=self.session.pk).update(
            scheduled_at=timezone.now() - dt.timedelta(hours=3)
        )
        self.session.refresh_from_db()
        self.assertFalse(policies.can_join(self.session))

    def test_a_cancelled_interview_has_no_room(self):
        InterviewSession.objects.filter(pk=self.session.pk).update(state=SessionState.CANCELLED)
        self.session.refresh_from_db()
        self.assertFalse(policies.can_join(self.session))


class SeedTests(TestCase):
    def test_the_platform_templates_seed_and_are_idempotent(self):
        call_command("seed_interview_templates", quiet_summary=True)
        first = InterviewTemplate.objects.filter(company__isnull=True).count()

        call_command("seed_interview_templates", quiet_summary=True)
        self.assertEqual(InterviewTemplate.objects.filter(company__isnull=True).count(), first)
        self.assertGreater(first, 0)

    def test_every_seeded_ai_template_has_questions(self):
        call_command("seed_interview_templates", quiet_summary=True)
        for template in InterviewTemplate.objects.filter(company__isnull=True, kind="ai_text"):
            with self.subTest(template=template.slug):
                self.assertTrue(template.question_pools)
                for competency in template.question_pools.values():
                    self.assertGreaterEqual(len(competency["questions"]), 3)


class PolicyEdgeTests(TaskFixture):
    """The refusal paths, which are the ones that matter in a policy module."""

    def setUp(self):
        super().setUp()
        self.application = application_for(self.profile, self.job)
        self.session = InterviewSession.objects.create(
            application=self.application,
            candidate=self.profile,
            template=self.template,
            kind="live",
            state=SessionState.SCHEDULED,
            scheduled_at=timezone.now(),
        )

    def test_an_employer_with_no_membership_is_not_found(self):
        from apps.common.errors import NotFound

        with self.assertRaises(NotFound):
            policies.require_employer(self.session, None)

    def test_require_joinable_passes_inside_the_window(self):
        self.assertEqual(policies.require_joinable(self.session), self.session)

    def test_require_joinable_refuses_outside_it(self):
        from apps.common.errors import InvalidState

        InterviewSession.objects.filter(pk=self.session.pk).update(
            scheduled_at=timezone.now() + dt.timedelta(hours=5)
        )
        self.session.refresh_from_db()

        with self.assertRaises(InvalidState):
            policies.require_joinable(self.session)

    def test_require_answerable_passes_an_open_session(self):
        InterviewSession.objects.filter(pk=self.session.pk).update(state=SessionState.IN_PROGRESS)
        self.session.refresh_from_db()
        self.assertEqual(policies.require_answerable(self.session), self.session)

    def test_require_answerable_refuses_a_finished_one(self):
        from apps.common.errors import InvalidState

        InterviewSession.objects.filter(pk=self.session.pk).update(state=SessionState.COMPLETED)
        self.session.refresh_from_db()

        with self.assertRaises(InvalidState):
            policies.require_answerable(self.session)

    def test_a_session_with_no_dates_is_inside_the_window(self):
        # A session that was never scheduled and never ended falls back to when
        # it was created. Refusing access to one because it has no end date
        # would lock an employer out of an interview they just created.
        InterviewSession.objects.filter(pk=self.session.pk).update(scheduled_at=None, ended_at=None)
        self.session.refresh_from_db()

        self.assertEqual(policies.require_employer(self.session, self.membership), self.session)

    def test_a_cancelled_interview_still_expires_from_its_scheduled_time(self):
        # A cancelled interview has no end date. Without the fallback its
        # company's access would never expire.
        from apps.common.errors import NotFound

        interviewer = _panel_interviewer(self.company, self.session)
        InterviewSession.objects.filter(pk=self.session.pk).update(
            state=SessionState.CANCELLED,
            ended_at=None,
            scheduled_at=timezone.now() - dt.timedelta(days=policies.ACCESS_DAYS_AFTER + 5),
        )
        self.session.refresh_from_db()

        with self.assertRaises(NotFound):
            policies.require_employer(self.session, interviewer)


class RoomProviderTests(TestCase):
    """The video seam (§11.3), both implementations.

    No network: `create_room` mints a reference and a token and returns them,
    which is the whole of what the scheduling service depends on. What a vendor
    does with the reference afterwards is the vendor's, and is not something a
    test here can honestly assert.
    """

    def test_the_fake_provider_creates_a_room(self):
        from apps.interviews.providers.fake import FakeRoomProvider

        room = FakeRoomProvider().create_room(
            subject="Backend Engineer", starts_at=timezone.now(), ends_at=timezone.now()
        )
        self.assertTrue(room.room_ref)
        self.assertTrue(room.token)
        self.assertEqual(room.provider, "fake")

    def test_jitsi_creates_an_unguessable_room(self):
        # A room named `acme-backend-engineer` is a room a stranger walks into
        # by typing it (§11.3).
        from apps.interviews.providers.jitsi import JitsiRoomProvider

        room = JitsiRoomProvider(app_id="vpaas-magic").create_room(
            subject="Backend Engineer at Acme",
            starts_at=timezone.now(),
            ends_at=timezone.now(),
        )
        self.assertNotIn("backend", room.room_ref.lower())
        self.assertNotIn("acme", room.room_ref.lower())
        self.assertGreater(len(room.room_ref), 20)

    def test_jitsi_returns_the_token_once_and_stores_only_its_hash(self):
        from apps.interviews.providers.base import constant_time_matches
        from apps.interviews.providers.jitsi import JitsiRoomProvider

        room = JitsiRoomProvider().create_room(
            subject="x", starts_at=timezone.now(), ends_at=timezone.now()
        )
        self.assertEqual(len(room.token_hash), 64)
        self.assertNotEqual(room.token, room.token_hash)
        self.assertTrue(constant_time_matches(room.token, room.token_hash))

    def test_a_wrong_token_does_not_match(self):
        from apps.interviews.providers.base import constant_time_matches, hash_token

        self.assertFalse(constant_time_matches("wrong", hash_token("right")))

    def test_two_rooms_never_share_a_reference(self):
        from apps.interviews.providers.jitsi import JitsiRoomProvider

        provider = JitsiRoomProvider()
        first = provider.create_room(subject="x", starts_at=None, ends_at=None)
        second = provider.create_room(subject="x", starts_at=None, ends_at=None)
        self.assertNotEqual(first.room_ref, second.room_ref)

    def test_jitsi_refuses_to_build_an_unsigned_join_url(self):
        # These two tests used to assert that a bare room URL came back, with
        # or without an app id. That URL was the defect: a bearer link anybody
        # could forward, carrying no identity and no expiry. The provider now
        # refuses without a signing key; signed URLs are covered in test_jitsi.
        from apps.interviews.providers import RoomProviderError
        from apps.interviews.providers.jitsi import JitsiRoomProvider

        for provider in (JitsiRoomProvider(app_id="vpaas-magic"), JitsiRoomProvider()):
            with self.subTest(app_id=provider.app_id), self.assertRaises(RoomProviderError):
                provider.join_url_for(room_ref="room-1", display_name="Priya", is_host=False)

    def test_the_configured_provider_is_the_fake_by_default(self):
        # A development machine with no JaaS credentials should schedule
        # interviews that work end to end rather than fail at the last step.
        from apps.interviews.providers import room_provider

        self.assertEqual(room_provider().key, "fake")


class ReminderSweepOutsideATransactionTests(TransactionTestCase):
    """The sweep as Celery actually runs it.

    `ReminderTests` above is a `TestCase`, so Django wraps each of its methods
    in a transaction and `events.emit`'s guard is satisfied by the test's own.
    A worker has no such wrapper: the sweep raised on the first session it
    found, and no interview reminder was ever delivered.

    So this is deliberately the same assertion in a class without the wrapper.
    """

    reset_sequences = True

    def setUp(self):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()

        FakeAdapter.reset()
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))

    def test_the_sweep_reminds_without_an_ambient_transaction(self):
        slot = scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[window(hours_ahead=24)]
        )[0]
        scheduling.book(
            slot=slot,
            application=application_for(create_profile(), self.job),
            actor=self.owner,
            template=live_template(slug="live-sweep"),
        )
        OutboxEvent.objects.all().delete()

        self.assertEqual(send_reminders()["reminded"], 1)
        self.assertTrue(OutboxEvent.objects.filter(type="interview.scheduled").exists())
