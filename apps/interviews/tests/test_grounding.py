"""
Whether a score rests on what the candidate actually said.

The evaluator is told to quote the candidate, and the candidate reads the
quotation. These tests hold the parts of that promise that can be checked
without trusting a model:

  * **a quotation that is not in the transcript never reaches the page** — the
    model is asked once more, and a second fabrication is struck out and the
    score marked ungrounded;
  * **"I don't know" is scored 1 without a model**, because there is nothing
    to read and a model asked to justify a score for nothing invents something;
  * **the level of the role reaches the evaluator**, so an intern and a senior
    are not held to one bar;
  * the verified evidence is stored rather than thrown away.

Model behaviour itself — retractions, contradictions, invented projects — is
measured against a real model by `manage.py interview_benchmark`, not here: the
fake adapter answers whatever it is queued, so a test of it would test the
queue.
"""

from __future__ import annotations

import json

from apps.ai.models import AIRun
from apps.ai.providers.fake import FakeAdapter
from apps.interviews.models import InterviewEvaluation, SessionState
from apps.interviews.services import engine, evaluation
from apps.interviews.tests.factories import FOLLOWUP_OUTPUT, fake_ai
from apps.interviews.tests.test_evaluation import EvaluationFixture

ANSWER = "I rebuilt the nightly reconciliation job and cut it from four hours to thirty minutes."


#: The per-answer judgement a scoring output carries: the first answer
#: addresses its question, in words it contains.
JUDGED = [{"n": 1, "addresses": "yes", "quote": "rebuilt the nightly reconciliation job"}]


class GroundingFixture(EvaluationFixture):
    def session_answering(self, *answers: str):
        """A completed session whose turns are answered in order, cycling."""
        with fake_ai():
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )

        index = 0
        while True:
            with fake_ai():
                FakeAdapter.queue(FOLLOWUP_OUTPUT)
                turn = engine.next_question(session=session)
            if turn is None:
                break
            engine.submit_answer(session=session, turn=turn, text=answers[index % len(answers)])
            index += 1

        session.state = SessionState.COMPLETED
        session.save(update_fields=["state", "updated_at"])
        FakeAdapter.reset()
        return session

    def evaluate(self, session, *outputs):
        with fake_ai():
            FakeAdapter.queue(*outputs)
            return evaluation.evaluate(session=session)


class QuotationTests(GroundingFixture):
    def test_a_real_quotation_is_kept_and_marked_grounded(self):
        session = self.session_answering(ANSWER)
        grounded = {
            "answers": JUDGED,
            "score": 4,
            "rationale": 'Explains the result: "cut it from four hours to thirty minutes".',
            "evidence": "cut it from four hours to thirty minutes",
        }

        self.evaluate(session, grounded, grounded)

        row = InterviewEvaluation.objects.filter(session=session).first()
        self.assertTrue(row.grounded)
        self.assertEqual(row.evidence, "cut it from four hours to thirty minutes")
        self.assertIn("four hours", row.rationale)

    def test_a_fabricated_quotation_is_asked_again_and_the_honest_answer_kept(self):
        session = self.session_answering(ANSWER)
        invented = {
            "answers": JUDGED,
            "score": 5,
            "rationale": 'Led the whole team: "I managed six engineers through the migration".',
            "evidence": "I managed six engineers",
        }
        honest = {
            "answers": JUDGED,
            "score": 3,
            "rationale": 'Describes their own change: "rebuilt the nightly reconciliation job".',
            "evidence": "rebuilt the nightly reconciliation job",
        }
        competencies = len(session.plan["competencies"])

        self.evaluate(session, *([invented, honest] * competencies))

        for row in InterviewEvaluation.objects.filter(session=session):
            self.assertTrue(row.grounded)
            self.assertEqual(row.score, 3)
            self.assertNotIn("six engineers", row.rationale)

    def test_a_quotation_invented_twice_is_struck_out_and_marked_ungrounded(self):
        session = self.session_answering(ANSWER)
        invented = {
            "answers": [{"n": 1, "addresses": "yes", "quote": "I managed six engineers"}],
            "score": 5,
            "rationale": 'Led the whole team: "I managed six engineers through the migration".',
            "evidence": "I managed six engineers",
        }

        self.evaluate(session, *([invented] * 10))

        row = InterviewEvaluation.objects.filter(session=session).first()
        self.assertFalse(row.grounded)
        self.assertNotIn("six engineers", row.rationale)
        self.assertIn("[not found in the answers]", row.rationale)
        self.assertEqual(row.evidence, "")
        # "Yes" resting on words the candidate never wrote is not full credit.
        self.assertLessEqual(row.score, 3)

    def test_words_from_the_question_are_not_the_candidates_words(self):
        # The question is in the transcript the model reads; a rationale that
        # quotes the question back as evidence has quoted nobody.
        session = self.session_answering(ANSWER)
        question = session.turns.first().question_text
        quoting_the_question = {
            "answers": JUDGED,
            "score": 4,
            "rationale": f'They said "{question}".',
        }

        self.evaluate(session, *([quoting_the_question] * 10))

        self.assertFalse(InterviewEvaluation.objects.filter(session=session).first().grounded)

    def test_punctuation_and_case_do_not_make_a_real_quotation_unfound(self):
        session = self.session_answering(ANSWER)
        loose = {
            "answers": JUDGED,
            "score": 4,
            "rationale": 'Said "Cut it from four hours, to thirty minutes!"',
        }

        self.evaluate(session, *([loose] * 10))

        self.assertTrue(InterviewEvaluation.objects.filter(session=session).first().grounded)


class NonAnswerTests(GroundingFixture):
    def test_i_dont_know_is_scored_one_without_asking_a_model(self):
        session = self.session_answering("I don't know.", "No idea, sorry.")

        written = self.evaluate(session)

        self.assertTrue(written)
        self.assertTrue(all(row.score == 1 for row in written))
        self.assertFalse(AIRun.objects.filter(task_key="interview.evaluate").exists())

    def test_a_one_word_answer_is_not_substantive(self):
        session = self.session_answering("Yes.")

        written = self.evaluate(session)

        self.assertTrue(all(row.score == 1 for row in written))

    def test_a_real_answer_that_starts_with_i_dont_know_is_still_read(self):
        session = self.session_answering(
            "I don't know the exact figure, but I rebuilt the reconciliation job and "
            "the close finished two hours earlier every night after that."
        )
        grounded = {
            "answers": JUDGED,
            "score": 3,
            "rationale": 'Said "rebuilt the reconciliation job".',
        }

        self.evaluate(session, *([grounded] * 10))

        self.assertTrue(AIRun.objects.filter(task_key="interview.evaluate").exists())

    def test_one_real_answer_among_non_answers_is_read(self):
        session = self.session_answering("I don't know.", ANSWER)
        grounded = {
            "answers": JUDGED,
            "score": 3,
            "rationale": 'Said "rebuilt the nightly reconciliation job".',
        }

        self.evaluate(session, *([grounded] * 10))

        self.assertTrue(AIRun.objects.filter(task_key="interview.evaluate").exists())


class LevelTests(GroundingFixture):
    def test_the_level_of_the_role_practised_for_reaches_the_evaluator(self):
        with fake_ai():
            FakeAdapter.queue()  # no kit written; the general pool is used
            session = engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                job=self.job,
            )
        self.assertIn("job_level", session.plan)

        turn = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=turn, text=ANSWER)
        session.state = SessionState.COMPLETED
        session.save(update_fields=["state", "updated_at"])
        FakeAdapter.reset()

        self.evaluate(
            session, {"answers": JUDGED, "score": 3, "rationale": 'Said "rebuilt the nightly".'}
        )

        run = AIRun.objects.filter(task_key="interview.evaluate").first()
        self.assertIn('"level"', json.dumps(run.input_snapshot))
