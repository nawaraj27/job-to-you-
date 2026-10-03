"""
Scoring, scorecards and outcomes (FR-10.1, FR-10.8, FR-10.10, FR-10.11).

The properties this file is answerable for, each asserted from the failing side:

  * **`integrity_meta` never reaches the evaluator.** Typing behaviour is not
    job performance; a paste count in a scoring prompt would be measuring the
    wrong thing with confidence;
  * **a scorecard is invisible until submitted** (FR-10.8) — the anchoring rule,
    asserted by looking with another interviewer's eyes;
  * every kind produces the same outcome shape (FR-10.10);
  * **practice results are the candidate's; an employer's are shared only when
    the employer shares them** (FR-10.11);
  * **nothing here moves an application** (ADR-A9).
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.ai.models import AIRun, RunStatus
from apps.ai.providers.base import ProviderFailure
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotFound
from apps.companies.models import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.interviews.models import InterviewEvaluation, SessionState
from apps.interviews.services import engine, evaluation
from apps.interviews.tests.factories import (
    EVALUATION_OUTPUT,
    FOLLOWUP_OUTPUT,
    fake_ai,
    live_template,
    practice_template,
)
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for
from apps.notifications.models import OutboxEvent


class EvaluationFixture(TestCase):
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

    def finished_session(self, *, application=None, integrity=None):
        """A completed session with every question answered."""
        with fake_ai():
            session = engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                application=application,
            )

        while True:
            with fake_ai():
                FakeAdapter.queue(FOLLOWUP_OUTPUT)
                turn = engine.next_question(session=session)
            if turn is None:
                break
            engine.submit_answer(
                session=session,
                turn=turn,
                text="I cut it from four hours to thirty minutes.",
                integrity=integrity,
            )

        session.state = SessionState.COMPLETED
        session.save(update_fields=["state", "updated_at"])

        # Building the session consumed queued answers on follow-up calls. A
        # test that then queues evaluation output would have it read by whatever
        # was left over, so the fake starts clean for the act under test.
        FakeAdapter.reset()
        return session


class AiScoringTests(EvaluationFixture):
    def test_every_competency_is_scored(self):
        session = self.finished_session()
        with fake_ai():
            FakeAdapter.queue(*[EVALUATION_OUTPUT] * 6)
            written = evaluation.evaluate(session=session)

        self.assertEqual(len(written), len(session.plan["competencies"]))
        self.assertTrue(all(row.by_ai for row in written))

    def test_an_ai_score_is_submitted_immediately(self):
        # There is nobody to anchor, and an unsubmitted AI score would be an
        # outcome that never arrives.
        session = self.finished_session()
        with fake_ai():
            FakeAdapter.queue(*[EVALUATION_OUTPUT] * 6)
            written = evaluation.evaluate(session=session)

        self.assertTrue(all(row.is_submitted for row in written))

    def test_scoring_goes_through_the_shared_runner(self):
        session = self.finished_session()
        with fake_ai():
            FakeAdapter.queue(*[EVALUATION_OUTPUT] * 6)
            evaluation.evaluate(session=session)

        runs = AIRun.objects.filter(task_key="interview.evaluate")
        self.assertTrue(runs.exists())
        self.assertTrue(all(run.status == RunStatus.OK for run in runs))

    def test_integrity_metadata_is_never_sent_for_scoring(self):
        # FR-10.4. The rule this module exists to enforce.
        session = self.finished_session(integrity={"pastes": 9, "blur_events": 7})
        with fake_ai():
            FakeAdapter.queue(*[EVALUATION_OUTPUT] * 6)
            evaluation.evaluate(session=session)

        for call in FakeAdapter.calls:
            with self.subTest(model=call["model"]):
                self.assertNotIn("paste", call["user"].lower())
                self.assertNotIn("blur", call["user"].lower())

    def test_the_transcript_carries_the_questions_and_answers(self):
        session = self.finished_session()
        with fake_ai():
            FakeAdapter.queue(*[EVALUATION_OUTPUT] * 6)
            evaluation.evaluate(session=session)

        sent = FakeAdapter.calls[0]["user"]
        self.assertIn("four hours", sent)
        self.assertIn("Question 1:", sent)
        self.assertIn("Answer 1:", sent)

    def test_a_failed_scoring_leaves_the_session_without_an_outcome(self):
        # An interview nobody read is not an interview somebody failed.
        session = self.finished_session()
        with fake_ai():
            FakeAdapter.queue(*[ProviderFailure("down")] * 12)
            written = evaluation.evaluate(session=session)

        self.assertEqual(written, [])
        self.assertIsNone(engine.outcome_for(session))

    def test_a_score_outside_the_rubric_is_discarded(self):
        session = self.finished_session()
        with fake_ai():
            FakeAdapter.queue(*[{"score": 9, "rationale": "Nonsense."}] * 6)
            written = evaluation.evaluate(session=session)

        self.assertEqual(written, [])

    def test_an_unfinished_session_cannot_be_scored(self):
        with fake_ai():
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )
        with self.assertRaises(InvalidState):
            evaluation.evaluate(session=session)

    def test_a_skipped_answer_is_not_scored_as_an_answer(self):
        with fake_ai():
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )
        while True:
            with fake_ai():
                turn = engine.next_question(session=session)
            if turn is None:
                break
            engine.skip(session=session, turn=turn)

        session.state = SessionState.COMPLETED
        session.save(update_fields=["state", "updated_at"])

        with fake_ai():
            self.assertEqual(evaluation.evaluate(session=session), [])


class ScorecardTests(EvaluationFixture):
    """FR-10.8, the anchoring rule."""

    def setUp(self):
        super().setUp()
        self.session = self.finished_session(application=application_for(self.profile, self.job))
        self.other = create_profile().user
        add_member(self.company, self.other, Role.INTERVIEWER)

    def test_an_interviewer_can_score_a_competency(self):
        row = evaluation.submit_scorecard(
            session=self.session,
            evaluator=self.owner,
            competency="problem_solving",
            score=4,
            rationale="Explained the trade-off.",
        )
        self.assertEqual(row.score, 4)
        self.assertTrue(row.is_submitted)
        self.assertFalse(row.by_ai)

    def test_a_score_outside_one_to_five_is_refused(self):
        for score in (0, 6, -1):
            with self.subTest(score=score), self.assertRaises(InvalidState):
                evaluation.submit_scorecard(
                    session=self.session,
                    evaluator=self.owner,
                    competency="problem_solving",
                    score=score,
                )

    def test_scoring_the_same_competency_twice_revises_rather_than_duplicates(self):
        # A second row would be averaged in, which is not what a revision means.
        evaluation.submit_scorecard(
            session=self.session, evaluator=self.owner, competency="ownership", score=2
        )
        evaluation.submit_scorecard(
            session=self.session, evaluator=self.owner, competency="ownership", score=4
        )

        rows = InterviewEvaluation.objects.filter(
            session=self.session, competency="ownership", evaluator=self.owner
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().score, 4)

    def test_two_interviewers_score_independently(self):
        evaluation.submit_scorecard(
            session=self.session, evaluator=self.owner, competency="ownership", score=2
        )
        evaluation.submit_scorecard(
            session=self.session, evaluator=self.other, competency="ownership", score=5
        )

        self.assertEqual(
            InterviewEvaluation.objects.filter(
                session=self.session, competency="ownership"
            ).count(),
            2,
        )

    def test_an_unsubmitted_scorecard_is_invisible_to_another_interviewer(self):
        # The anchoring rule. Written directly rather than through the service,
        # because the service always submits — the draft state exists for a
        # future edit flow and the visibility rule must hold for it now.
        InterviewEvaluation.objects.create(
            session=self.session,
            competency="ownership",
            evaluator=self.owner,
            score=5,
            submitted_at=None,
        )
        visible = evaluation.visible_evaluations(session=self.session, viewer=self.other)
        self.assertEqual(list(visible), [])

    def test_an_interviewer_can_see_their_own_unsubmitted_scorecard(self):
        row = InterviewEvaluation.objects.create(
            session=self.session,
            competency="ownership",
            evaluator=self.owner,
            score=5,
            submitted_at=None,
        )
        visible = evaluation.visible_evaluations(session=self.session, viewer=self.owner)
        self.assertIn(row, visible)

    def test_a_submitted_scorecard_is_visible_to_everybody(self):
        row = evaluation.submit_scorecard(
            session=self.session, evaluator=self.owner, competency="ownership", score=3
        )
        visible = evaluation.visible_evaluations(session=self.session, viewer=self.other)
        self.assertIn(row, visible)

    def test_scoring_is_audited(self):
        from apps.audit.models import AuditLog

        evaluation.submit_scorecard(
            session=self.session, evaluator=self.owner, competency="ownership", score=3
        )
        self.assertTrue(AuditLog.objects.filter(verb="interview.scored").exists())


class OutcomeTests(EvaluationFixture):
    """FR-10.10 and FR-10.11."""

    def setUp(self):
        super().setUp()
        self.application = application_for(self.profile, self.job)
        self.session = self.finished_session(application=self.application)

    def score(self, *scores: int) -> None:
        for index, score in enumerate(scores):
            evaluation.submit_scorecard(
                session=self.session,
                evaluator=self.owner,
                competency=f"competency_{index}",
                score=score,
            )

    def test_an_outcome_averages_the_submitted_scores(self):
        self.score(4, 4, 5)
        outcome = evaluation.conclude(session=self.session, actor=self.owner)

        self.assertEqual(float(outcome.average_score), 4.33)
        self.assertEqual(outcome.recommendation, "strong_yes")

    def test_an_unsubmitted_score_is_not_counted(self):
        # An unsubmitted draft is not an opinion yet, and counting it would mean
        # the outcome changed as somebody typed.
        self.score(2)
        InterviewEvaluation.objects.create(
            session=self.session,
            competency="draft",
            evaluator=create_profile().user,
            score=5,
            submitted_at=None,
        )
        outcome = evaluation.conclude(session=self.session, actor=self.owner)
        self.assertEqual(float(outcome.average_score), 2.0)

    def test_concluding_with_nothing_scored_is_refused(self):
        with self.assertRaises(InvalidState):
            evaluation.conclude(session=self.session, actor=self.owner)

    def test_the_summary_names_the_strong_and_weak_competencies(self):
        self.score(5, 1)
        outcome = evaluation.conclude(session=self.session, actor=self.owner)

        self.assertIn("competency_0", outcome.summary)
        self.assertIn("competency_1", outcome.summary)

    def test_an_outcome_is_one_per_session(self):
        self.score(3)
        first = evaluation.conclude(session=self.session, actor=self.owner)
        self.score(5)
        second = evaluation.conclude(session=self.session, actor=self.owner)

        self.assertEqual(first.pk, second.pk)

    def test_an_outcome_does_not_move_the_application(self):
        # ADR-A9. An interview recommends; a person decides.
        stage_before = self.application.stage_id
        self.score(1, 1)
        evaluation.conclude(session=self.session, actor=self.owner)

        self.application.refresh_from_db()
        self.assertEqual(self.application.stage_id, stage_before)


class SharingTests(EvaluationFixture):
    """FR-10.11."""

    def setUp(self):
        super().setUp()
        self.application = application_for(self.profile, self.job)
        self.session = self.finished_session(application=self.application)
        evaluation.submit_scorecard(
            session=self.session, evaluator=self.owner, competency="ownership", score=4
        )
        self.outcome = evaluation.conclude(session=self.session, actor=self.owner)

    def test_an_employer_outcome_is_not_shared_by_default(self):
        self.assertFalse(self.outcome.shared_with_candidate)
        self.assertIsNone(evaluation.readable_by_candidate(self.session))

    def test_sharing_makes_it_readable_by_the_candidate(self):
        evaluation.share_with_candidate(session=self.session, actor=self.owner)
        self.session.refresh_from_db()

        self.assertIsNotNone(evaluation.readable_by_candidate(self.session))

    def test_sharing_emits_an_event(self):
        evaluation.share_with_candidate(session=self.session, actor=self.owner)
        self.assertTrue(OutboxEvent.objects.filter(type="interview.outcome_shared").exists())

    def test_sharing_twice_changes_nothing(self):
        first = evaluation.share_with_candidate(session=self.session, actor=self.owner)
        shared_at = first.shared_at
        second = evaluation.share_with_candidate(session=self.session, actor=self.owner)
        self.assertEqual(second.shared_at, shared_at)

    def test_a_session_with_no_outcome_cannot_be_shared(self):
        # A second job, because `self.profile` has already applied to the first
        # and one live application per job is a database constraint (P8).
        second_job = publish(create_job(self.company))
        other = self.finished_session(application=application_for(self.profile, second_job))

        with self.assertRaises(NotFound):
            evaluation.share_with_candidate(session=other, actor=self.owner)

    def test_practice_results_are_always_the_candidates(self):
        # FR-10.11. No employer to ask, so nothing to share.
        practice = self.finished_session()
        evaluation.submit_scorecard(
            session=practice, evaluator=self.profile.user, competency="ownership", score=3
        )
        evaluation.conclude(session=practice)

        self.assertIsNotNone(evaluation.readable_by_candidate(practice))

    def test_sharing_is_audited(self):
        from apps.audit.models import AuditLog

        evaluation.share_with_candidate(session=self.session, actor=self.owner)
        self.assertTrue(AuditLog.objects.filter(verb="interview.outcome_shared").exists())


class UniformShapeTests(EvaluationFixture):
    """FR-10.10: every kind produces the same outcome shape."""

    def test_a_live_interview_produces_the_same_outcome_as_an_ai_one(self):
        from apps.interviews.models import InterviewKind, InterviewSession

        live = InterviewSession.objects.create(
            application=application_for(self.profile, self.job),
            candidate=self.profile,
            template=live_template(),
            kind=InterviewKind.LIVE,
            state=SessionState.COMPLETED,
        )
        evaluation.submit_scorecard(
            session=live, evaluator=self.owner, competency="problem_solving", score=4
        )
        outcome = evaluation.conclude(session=live, actor=self.owner)

        self.assertEqual(outcome.recommendation, "yes")
        self.assertEqual(float(outcome.average_score), 4.0)
        self.assertTrue(hasattr(outcome, "shared_with_candidate"))
