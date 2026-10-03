"""
A finished interview that was never scored is asked for again, and then told honestly.

Scoring is queued once, when the interview ends. With the model down, a worker
lost or the broker dropping the message, nothing asked again and the result
page said "your answers are being read" for ever. And a scoring task that ran
out of time was not told so: the runner recorded Celery's soft-time-limit
signal as a provider error and moved on to its next model call.
"""

from __future__ import annotations

import datetime as dt
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import runner
from apps.interviews import tasks
from apps.interviews.models import InterviewOutcome, SessionState
from apps.interviews.services import evaluation

from .factories import EVALUATION_OUTPUT, fake_ai
from .test_evaluation import EvaluationFixture


class RecoverySweepTests(EvaluationFixture):
    def setUp(self):
        super().setUp()
        cache.clear()

    def ended(self, session, minutes_ago):
        session.ended_at = timezone.now() - dt.timedelta(minutes=minutes_ago)
        session.save(update_fields=["ended_at", "updated_at"])
        return session

    def test_a_stalled_unscored_interview_is_scored_by_the_sweep(self):
        session = self.ended(self.finished_session(), 30)

        with fake_ai():
            FakeAdapter.queue(*[EVALUATION_OUTPUT] * 10)
            result = tasks.recover_unscored_sessions()

        self.assertEqual(result["requeued"], 1)
        self.assertTrue(session.evaluations.exists())

    def test_only_stalled_unscored_self_paced_interviews_are_asked_again(self):
        # Three practice sessions in a month is beyond Free's two (ADR-A59).
        from apps.billing.tests.factories import put_on_pro

        put_on_pro(self.profile)
        recent = self.ended(self.finished_session(), 5)
        too_old = self.ended(self.finished_session(), 60 * 30)
        scored = self.ended(self.finished_session(), 30)
        evaluation._write_ai_score(
            session=scored, key="x", score=3, rationale="", evidence="", grounded=True, run=None
        )

        with mock.patch.object(tasks.evaluate_session, "delay") as delay:
            tasks.recover_unscored_sessions()

        asked = {call.args[0] for call in delay.call_args_list}
        self.assertNotIn(recent.pk, asked)
        self.assertNotIn(too_old.pk, asked)
        self.assertNotIn(scored.pk, asked)

    def test_a_session_is_asked_for_at_most_three_times(self):
        session = self.ended(self.finished_session(), 30)

        with mock.patch.object(tasks.evaluate_session, "delay") as delay:
            for _ in range(5):
                tasks.recover_unscored_sessions()

        self.assertEqual(
            [call.args[0] for call in delay.call_args_list],
            [session.pk] * tasks.MAX_SCORING_ATTEMPTS,
        )

    def test_the_sweep_is_scheduled(self):
        from config.celery import app

        scheduled = {entry["task"] for entry in app.conf.beat_schedule.values()}
        self.assertIn("apps.interviews.tasks.recover_unscored_sessions", scheduled)


class TimeLimitTests(EvaluationFixture):
    def test_the_runner_lets_the_soft_time_limit_through(self):
        with fake_ai():
            FakeAdapter.queue(SoftTimeLimitExceeded())
            with self.assertRaises(SoftTimeLimitExceeded):
                runner.run(task_key="interview.evaluate", inputs={"transcript": "x"})

    def test_scoring_that_runs_out_of_time_keeps_what_it_scored(self):
        session = self.finished_session()

        def partly(session):
            evaluation._write_ai_score(
                session=session,
                key="first",
                score=3,
                rationale="Scored.",
                evidence="",
                grounded=True,
                run=None,
            )
            raise SoftTimeLimitExceeded()

        with mock.patch.object(evaluation, "evaluate", side_effect=partly):
            result = tasks.evaluate_session(session.pk)

        self.assertEqual(result, "timed-out")
        self.assertTrue(InterviewOutcome.objects.filter(session=session).exists())

    def test_scoring_has_room_for_a_slow_local_model(self):
        self.assertGreater(tasks.evaluate_session.soft_time_limit, 540)
        self.assertGreater(tasks.plan_job_interview.soft_time_limit, 540)


class ResultPageTests(EvaluationFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.profile.user)

    def page(self, session):
        return self.client.get(
            reverse("interviews:result", kwargs={"public_id": session.public_id})
        )

    def test_a_recently_finished_unscored_interview_is_being_read(self):
        session = self.finished_session()
        session.ended_at = timezone.now() - dt.timedelta(minutes=10)
        session.save(update_fields=["ended_at"])

        response = self.page(session)

        self.assertContains(response, "being read")
        self.assertContains(response, 'hx-trigger="every 5s"')

    def test_an_interview_nobody_could_score_says_so_and_stops_polling(self):
        session = self.finished_session()
        session.ended_at = timezone.now() - dt.timedelta(hours=tasks.SCORING_RETRY_HOURS + 1)
        session.save(update_fields=["ended_at"])

        response = self.page(session)

        self.assertContains(response, "We could not read your answers this time.")
        self.assertNotContains(response, "being read")
        self.assertNotContains(response, 'hx-trigger="every 5s"')
        self.assertEqual(session.state, SessionState.COMPLETED)
