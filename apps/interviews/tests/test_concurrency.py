"""
Two people taking the same interview time at the same moment, on real Postgres.

`TestCase` wraps every test in one transaction, so two "concurrent" bookings in
it are not concurrent at all. These run in `TransactionTestCase` with two
threads and two database connections, which is the only way the exclusion
constraint and `select_for_update` are actually exercised.
"""

from __future__ import annotations

import threading

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from apps.ai.services import prompts
from apps.candidates.tests.factories import create_profile
from apps.common.errors import DomainError
from apps.companies.tests.factories import company_with_owner
from apps.interviews.models import InterviewSession, InterviewSlot, SlotState
from apps.interviews.services import scheduling
from apps.interviews.tests.factories import live_template, window
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for


class ConcurrentBookingTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.template = live_template()
        scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[window(hours_ahead=30)]
        )
        self.slot = InterviewSlot.objects.get(interviewer=self.membership)
        self.first = application_for(create_profile(), self.job)
        self.second = application_for(create_profile(), self.job)

    def _race(self, action):
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def run(application):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                action(application)
                outcomes.append("booked")
            except DomainError as exc:
                outcomes.append(f"refused: {exc.message}")
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(app,)) for app in (self.first, self.second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return outcomes

    def test_two_candidates_booking_one_slot_at_once_get_one_booking(self):
        def book(application):
            scheduling.book(
                slot=self.slot,
                application=application,
                actor=application.candidate.user,
                template=self.template,
            )

        outcomes = self._race(book)

        self.assertEqual(sorted(o.split(":")[0] for o in outcomes), ["booked", "refused"])
        self.assertEqual(InterviewSession.objects.count(), 1)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.state, SlotState.BOOKED)

    def test_two_holds_on_one_slot_at_once_give_one_hold(self):
        def hold(application):
            scheduling.hold(slot=self.slot, actor=application.candidate.user)

        outcomes = self._race(hold)

        self.assertEqual(sorted(o.split(":")[0] for o in outcomes), ["booked", "refused"])

    def test_an_overlapping_slot_is_refused_by_the_database(self):
        starts = self.slot.starts_at
        with self.assertRaises(DomainError):
            scheduling.publish_slots(
                membership=self.membership,
                actor=self.owner,
                windows=[(starts, self.slot.ends_at)],
            )
        self.assertEqual(InterviewSlot.objects.filter(interviewer=self.membership).count(), 1)


class ConcurrentScoringTests(TransactionTestCase):
    """Two evaluations of one finished interview, landing at the same moment.

    A retried Celery task, or a second worker, is enough to run `evaluate`
    twice. The model's scores have no evaluator, and the one-per-evaluator
    constraint treated those NULLs as distinct: two rows per competency, both
    averaged into the outcome.
    """

    reset_sequences = True

    def setUp(self):
        from apps.interviews.models import SessionState
        from apps.interviews.tests.factories import practice_template

        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()
        profile = create_profile()
        self.session = InterviewSession.objects.create(
            candidate=profile,
            template=practice_template(),
            kind="ai_text",
            state=SessionState.COMPLETED,
        )

    def test_two_scores_written_at_once_leave_one_row_per_competency(self):
        from apps.interviews.models import InterviewEvaluation, InterviewOutcome
        from apps.interviews.services import evaluation

        barrier = threading.Barrier(2)
        errors: list[str] = []

        def score(value):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                evaluation._write_ai_score(
                    session=self.session,
                    key="problem_solving",
                    score=value,
                    rationale=f"Scored {value}.",
                    evidence="",
                    grounded=True,
                    run=None,
                )
                evaluation.conclude(session=self.session)
            except Exception as exc:  # noqa: BLE001 - the assertion reports it
                errors.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=score, args=(value,)) for value in (2, 4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        rows = InterviewEvaluation.objects.filter(session=self.session, evaluator__isnull=True)
        self.assertEqual(rows.count(), 1)
        self.assertIn(rows.get().score, (2, 4))
        self.assertEqual(InterviewOutcome.objects.filter(session=self.session).count(), 1)

    def test_the_database_refuses_a_second_ai_row(self):
        from django.db import IntegrityError, transaction

        from apps.interviews.models import InterviewEvaluation

        InterviewEvaluation.objects.create(session=self.session, competency="x", score=3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            InterviewEvaluation.objects.create(session=self.session, competency="x", score=4)
