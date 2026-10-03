"""
Three AI defects measured on the live model, held here without a model.

  * **A confidently wrong answer scored 3** ("POST is idempotent by definition",
    2 of 2 benchmark runs). The evaluator now lists wrong statements, and a
    listed error that quotes the candidate caps the score at 2.
  * **Follow-ups ignored a self-correction** ("actually, my manager led it — I
    wrote the rollback scripts" produced a question about the team's migration
    decisions, 2 of 2 runs). The corrected part is passed to the model, and a
    follow-up that shares nothing with it is asked for once more.
  * **37% of stored competencies did not name the posting.** Anchored questions
    are served first, and a poorly anchored kit is rewritten off the request.

The fake adapter answers what it is queued, so these hold the code's handling
of model output. Whether the model *produces* the right output is measured by
`manage.py interview_benchmark --live`.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from apps.ai.models import AIRun
from apps.ai.providers.fake import FakeAdapter
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.interviews.models import InterviewEvaluation, InterviewTemplate, QuestionOrigin
from apps.interviews.services import engine, planning, relevance
from apps.interviews.tests.factories import fake_ai
from apps.interviews.tests.test_engine import EngineFixture
from apps.interviews.tests.test_grounding import GroundingFixture
from apps.jobs.tests.factories import create_job, publish

WRONG = (
    "I made the payout endpoint safe by switching it from PUT to POST, because POST "
    "requests are idempotent by definition, so clients can retry as often as they like."
)


#: The per-answer judgement every scoring output now carries.
ANSWERED = [{"n": 1, "addresses": "yes", "quote": "switching it from PUT to POST"}]


class WrongClaimTests(GroundingFixture):
    def test_a_grounded_error_caps_the_score(self):
        session = self.session_answering(WRONG)
        output = {
            "answers": ANSWERED,
            "score": 4,
            "rationale": 'Concrete: "switching it from PUT to POST".',
            "errors": [
                {
                    "quote": "POST requests are idempotent by definition",
                    "problem": "POST is not idempotent; PUT is.",
                }
            ],
        }

        self.evaluate(session, *([output] * 10))

        row = InterviewEvaluation.objects.filter(session=session).first()
        self.assertEqual(row.score, 2)
        self.assertIn("Score limited to 2", row.rationale)
        self.assertIn("POST is not idempotent", row.rationale)

    def test_an_error_the_candidate_never_said_costs_nothing(self):
        session = self.session_answering(WRONG)
        output = {
            "answers": ANSWERED,
            "score": 4,
            "rationale": 'Concrete: "switching it from PUT to POST".',
            "errors": [{"quote": "we stored passwords in plain text", "problem": "insecure"}],
        }

        self.evaluate(session, *([output] * 10))

        self.assertEqual(InterviewEvaluation.objects.filter(session=session).first().score, 4)

    def test_no_errors_leaves_the_score_alone(self):
        session = self.session_answering(WRONG)
        output = {
            "answers": ANSWERED,
            "score": 4,
            "rationale": 'Said "switching it from PUT to POST".',
            "errors": [],
        }

        self.evaluate(session, *([output] * 10))

        self.assertEqual(InterviewEvaluation.objects.filter(session=session).first().score, 4)

    def test_a_low_score_is_not_raised_by_the_cap(self):
        session = self.session_answering(WRONG)
        output = {
            "answers": ANSWERED,
            "score": 1,
            "rationale": 'Said "switching it from PUT to POST".',
            "errors": [{"quote": "idempotent by definition", "problem": "wrong"}],
        }

        self.evaluate(session, *([output] * 10))

        self.assertEqual(InterviewEvaluation.objects.filter(session=session).first().score, 1)


class CorrectionParsingTests(TestCase):
    def test_the_part_after_a_correction_is_what_they_did(self):
        self.assertEqual(
            engine.corrected_part(
                "I led the migration. Actually, let me correct that: my manager led it "
                "and I wrote the rollback scripts."
            ),
            "my manager led it and I wrote the rollback scripts",
        )

    def test_the_last_correction_wins(self):
        self.assertEqual(
            engine.corrected_part(
                "It was March. Actually, it was April. No, wait, it was the week we moved offices."
            ),
            "it was the week we moved offices",
        )

    def test_a_short_aside_is_not_a_correction(self):
        self.assertEqual(engine.corrected_part("Actually yes."), "")

    def test_no_marker_means_no_correction(self):
        self.assertEqual(engine.corrected_part("I wrote the rollback scripts myself."), "")


class CorrectionFollowupTests(EngineFixture):
    CORRECTED = (
        "I led the database migration. Actually, let me correct that: my manager led it "
        "and I wrote the rollback scripts and ran them during cutover."
    )

    def reach_followup(self, *queued):
        session = self.start()
        with fake_ai():
            first = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=first, text="I shipped the first part.")
        with fake_ai():
            second = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=second, text=self.CORRECTED)
        FakeAdapter.reset()
        with fake_ai():
            FakeAdapter.queue(*queued)
            return session, engine.next_question(session=session)

    def test_the_correction_is_given_to_the_model(self):
        self.reach_followup({"question": "How did you test the rollback scripts before cutover?"})

        run = AIRun.objects.filter(task_key="interview.followup").latest("created_at")
        self.assertIn("rollback scripts", run.input_snapshot["correction_note"])

    def test_a_followup_that_ignores_the_correction_is_asked_again(self):
        _, turn = self.reach_followup(
            {"question": "What decisions did your team make before starting the migration?"},
            {"question": "How did you test the rollback scripts before cutover?"},
        )

        self.assertEqual(turn.question_origin, QuestionOrigin.AI_FOLLOWUP)
        self.assertIn("rollback", turn.question_text)
        self.assertEqual(AIRun.objects.filter(task_key="interview.followup").count(), 2)

    def test_twice_ignored_moves_on_to_a_planned_question(self):
        ignoring = {"question": "What decisions did your team make before starting?"}
        _, turn = self.reach_followup(ignoring, ignoring)

        self.assertEqual(turn.question_origin, QuestionOrigin.POOL)

    def test_an_answer_without_a_correction_is_asked_once(self):
        session = self.start()
        with fake_ai():
            first = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=first, text="I shipped it.")
        with fake_ai():
            second = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=second, text="I wrote the rollback scripts.")
        FakeAdapter.reset()
        with fake_ai():
            FakeAdapter.queue({"question": "What would you change next time?"})
            engine.next_question(session=session)

        self.assertEqual(AIRun.objects.filter(task_key="interview.followup").count(), 1)


class RelevanceServingTests(TestCase):
    """Serving by the job-specificity rule validated on hand labels.

    Replaces the lexical anchoring tests. The lexical rule scored 0.765 accuracy
    on 81 labelled questions and served only anchored questions, which left
    some competencies with two questions and every candidate the same pair. The
    rule now keeps a pool of three when it can and never serves a question
    borrowed from another posting.
    """

    def setUp(self):
        cache.clear()
        owner = create_profile().user
        self.company, _ = company_with_owner(owner)
        self.job = publish(create_job(self.company))
        self.job.title = "Freight Operations Analyst"
        self.job.description = (
            "Track late freight shipments across depots and explain what each delay cost."
        )
        self.job.save()
        other = publish(create_job(self.company))
        other.title = "Payments Engineer"
        other.description = "Own the settlement ledger, reconciliation and the nightly close."
        other.save()

    def kit(self, questions):
        return InterviewTemplate.objects.create(
            company=self.company,
            slug=planning.slug_for(self.job),
            name="kit",
            kind="ai_text",
            question_pools={"delivery": {"label": "Delivery", "why": "", "questions": questions}},
            config={"source": planning.AUTO_SOURCE, "source_sha256": planning._digest(self.job)},
        )

    def test_stems_match_equivalent_word_forms(self):
        terms = relevance.Posting.from_job(self.job).terms()
        self.assertTrue(
            relevance.lexical_specific("Tell me about a shipment that arrived late.", terms)
        )
        self.assertFalse(
            relevance.lexical_specific("Tell me about a time you led a project.", terms)
        )

    def test_specific_questions_come_first_and_the_pool_keeps_three(self):
        self.kit(
            [
                "Tell me about a time you led a project.",
                "Tell me about a freight shipment that arrived late at a depot.",
                "Describe a delay you had to explain to a customer.",
            ]
        )
        with mock.patch("apps.interviews.tasks.plan_job_interview.delay"):
            pools = planning.cached_pools(self.job)

        served = pools["delivery"]["questions"]
        self.assertEqual(len(served), 3)
        self.assertEqual(served[-1], "Tell me about a time you led a project.")

    def test_a_generic_question_is_dropped_when_there_are_enough_specific_ones(self):
        self.kit(
            [
                "Tell me about a time you led a project.",
                "Tell me about a freight shipment that arrived late at a depot.",
                "Describe a delay you had to explain to a customer.",
                "Tell me about a depot that kept missing its dispatch times.",
            ]
        )
        with mock.patch("apps.interviews.tasks.plan_job_interview.delay"):
            pools = planning.cached_pools(self.job)

        self.assertNotIn("Tell me about a time you led a project.", pools["delivery"]["questions"])

    def test_a_question_borrowed_from_another_posting_is_never_served(self):
        self.kit(
            [
                "Tell me about a time you resolved a discrepancy in the nightly close.",
                "Tell me about a freight shipment that arrived late at a depot.",
                "Describe a delay you had to explain to a customer.",
            ]
        )
        with mock.patch("apps.interviews.tasks.plan_job_interview.delay") as queued:
            pools = planning.cached_pools(self.job)

        self.assertNotIn(
            "Tell me about a time you resolved a discrepancy in the nightly close.",
            pools["delivery"]["questions"],
        )
        queued.assert_called_once_with(self.job.pk, replace=True)

    def test_a_mostly_generic_kit_is_queued_for_rewriting(self):
        self.kit(
            [
                "Tell me about a time you led a project.",
                "Describe feedback that changed how you work.",
                "Tell me about a deadline you missed.",
            ]
        )
        with mock.patch("apps.interviews.tasks.plan_job_interview.delay") as queued:
            planning.cached_pools(self.job)

        queued.assert_called_once_with(self.job.pk, replace=True)

    def test_a_relevant_kit_is_not_rewritten(self):
        self.kit(
            [
                "Tell me about a freight shipment that arrived late at a depot.",
                "Describe a delay you had to explain to a customer.",
                "Tell me about a depot that kept missing its dispatch times.",
            ]
        )
        with mock.patch("apps.interviews.tasks.plan_job_interview.delay") as queued:
            planning.cached_pools(self.job)

        queued.assert_not_called()

    def test_a_competency_left_with_two_questions_is_queued_for_rewriting(self):
        """Some served pools were 2, so every candidate got the same pair there.

        The kit below is relevant (every question names the posting), so the
        relevance rule alone never rewrote it.
        """
        self.kit(
            [
                "Tell me about a freight shipment that arrived late at a depot.",
                "Describe a delay you had to explain to a customer.",
            ]
        )
        with mock.patch("apps.interviews.tasks.plan_job_interview.delay") as queued:
            pools = planning.cached_pools(self.job)

        self.assertEqual(len(pools["delivery"]["questions"]), 2)
        self.assertEqual(planning.thin_pools(pools), ["delivery"])
        queued.assert_called_once_with(self.job.pk, replace=True)

    def test_filtering_that_leaves_two_also_counts_as_thin(self):
        self.kit(
            [
                "Tell me about a freight shipment that arrived late at a depot.",
                "Describe a delay you had to explain to a customer.",
                "How would you handle a depot that kept missing its dispatch times?",
            ]
        )
        with mock.patch("apps.interviews.tasks.plan_job_interview.delay") as queued:
            planning.cached_pools(self.job)

        queued.assert_called_once_with(self.job.pk, replace=True)

    def test_a_rewrite_is_throttled_to_once_a_day(self):
        from apps.interviews.tasks import plan_job_interview

        self.kit(["Tell me about a time you worked in a team."] * 3)
        with fake_ai(), mock.patch.object(planning, "plan_job", return_value=None) as plan:
            first = plan_job_interview(self.job.pk, replace=True)
            second = plan_job_interview(self.job.pk, replace=True)

        self.assertEqual(first, "unavailable")
        self.assertEqual(second, "replan-throttled")
        plan.assert_called_once()


class PlanSchemaRetryTests(TestCase):
    """A kit with one competency was thrown away after two minutes of generation."""

    def setUp(self):
        from django.core.management import call_command

        from apps.ai.services import prompts

        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()
        FakeAdapter.reset()
        owner = create_profile().user
        self.company, _ = company_with_owner(owner)
        self.job = publish(create_job(self.company))

    ONE_COMPETENCY = {
        "competencies": [
            {
                "key": "billing",
                "label": "Billing",
                "why": "",
                "questions": [
                    "Tell me about a billing service change.",
                    "Describe a billing incident.",
                    "Tell me about a billing bug.",
                ],
            }
        ]
    }
    TWO_COMPETENCIES = {
        "competencies": [
            {
                "key": "billing",
                "label": "Billing",
                "why": "",
                "questions": [
                    "Tell me about a billing service change.",
                    "Describe a billing incident.",
                    "Tell me about a billing bug.",
                ],
            },
            {
                "key": "delivery",
                "label": "Delivery",
                "why": "",
                "questions": [
                    "Tell me about a billing release.",
                    "Describe a billing rollback.",
                    "Tell me about a billing migration.",
                ],
            },
        ]
    }

    def test_a_kit_that_fails_its_schema_is_asked_for_once_more(self):
        with fake_ai():
            FakeAdapter.queue(self.ONE_COMPETENCY, self.TWO_COMPETENCIES)
            template = planning.plan_job(job=self.job)

        self.assertIsNotNone(template)
        self.assertEqual(sorted(template.question_pools), ["billing", "delivery"])
        self.assertEqual(AIRun.objects.filter(task_key="interview.plan").count(), 2)

    def test_it_is_asked_only_once_more(self):
        with fake_ai():
            FakeAdapter.queue(self.ONE_COMPETENCY, self.ONE_COMPETENCY, self.TWO_COMPETENCIES)
            template = planning.plan_job(job=self.job)

        self.assertIsNone(template)
        self.assertEqual(AIRun.objects.filter(task_key="interview.plan").count(), 2)


class NearDuplicateTests(TestCase):
    def test_a_question_restated_in_the_same_competency_is_served_once(self):
        pools = planning._pools_from(
            {
                "competencies": [
                    {
                        "key": "billing",
                        "label": "Billing",
                        "why": "",
                        "questions": [
                            "Tell me about a billing service change you shipped.",
                            "Describe a billing service change you shipped and what happened.",
                            "Tell me about a refund that was recorded twice.",
                        ],
                    }
                ]
            }
        )

        self.assertEqual(
            pools["billing"]["questions"],
            [
                "Tell me about a billing service change you shipped.",
                "Tell me about a refund that was recorded twice.",
            ],
        )

    def test_different_questions_on_one_subject_are_kept(self):
        questions = [
            "Tell me about a ledger that did not balance at month end.",
            "Describe a payout you had to reverse after it was sent.",
            "Tell me about a nightly close that overran.",
        ]
        pools = planning._pools_from(
            {"competencies": [{"key": "k", "label": "", "why": "", "questions": questions}]}
        )
        self.assertEqual(pools["k"]["questions"], questions)


class OneQuestionTests(TestCase):
    """The local model asked two questions in one follow-up (2 of 32 benchmark runs)."""

    @classmethod
    def setUpTestData(cls):
        from apps.ai.services import prompts

        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()

    def test_the_second_question_is_dropped(self):
        self.assertEqual(
            engine.one_question(
                "Can you tell me more about the rollback process you handled? What challenges did you face?"
            ),
            "Can you tell me more about the rollback process you handled?",
        )

    def test_a_single_question_is_unchanged(self):
        text = "How did you test the rollback scripts before cutover?"
        self.assertEqual(engine.one_question(text), text)

    def test_text_without_a_question_mark_is_unchanged(self):
        self.assertEqual(
            engine.one_question("Tell me about the rollback."), "Tell me about the rollback."
        )

    def test_a_follow_up_is_served_as_one_question(self):
        with fake_ai():
            FakeAdapter.queue({"question": "What did you change? And why did you change it?"})
            question, _run = engine.write_followup(
                competency="Delivery",
                question="Tell me about a release.",
                answer="I rolled it back.",
            )
        self.assertEqual(question, "What did you change?")
