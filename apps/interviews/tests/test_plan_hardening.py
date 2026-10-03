"""
The interview kit when the model misbehaves, and the questions it writes.

Two things are pinned here:

  * **Malformed output never reaches a candidate.** Invalid or truncated JSON,
    missing fields, wrong types, an empty pool or a pool of duplicates leaves
    the posting on its job-aware fallback, and a practice session still opens
    and asks questions about the job. Extra fields are ignored. A second
    failure is not followed by a third attempt.
  * **Questions do not hand the candidate the employer's own history.** "The
    guest-facing booking app" is this employer's product; the candidate has
    never worked on it. Such questions are rewritten to "a guest-facing booking
    app", which is what the plan prompt asks for.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.ai.models import AIRun
from apps.ai.providers.fake import FakeAdapter
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.interviews.models import InterviewTemplate
from apps.interviews.services import engine, planning, relevance
from apps.jobs.tests.factories import create_job, publish

from .factories import fake_ai


def _competency(key: str, questions: list) -> dict:
    return {"key": key, "label": key.title(), "why": "", "questions": questions}


GOOD = {
    "competencies": [
        _competency(
            "billing",
            [
                "Tell me about a billing service change you shipped.",
                "Describe a billing incident you handled.",
                "Tell me about a billing bug you fixed.",
            ],
        ),
        _competency(
            "delivery",
            [
                "Tell me about a billing release you ran.",
                "Describe a billing rollback you made.",
                "Tell me about a billing migration you led.",
            ],
        ),
    ]
}


class MalformedKitTests(TestCase):
    def setUp(self):
        from apps.ai.services import prompts

        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_interview_templates")
        prompts.load_all()
        FakeAdapter.reset()
        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company, title="Billing Engineer"))

    def plan(self, *answers):
        with fake_ai():
            FakeAdapter.queue(*answers)
            return planning.plan_job(job=self.job)

    def start_session(self):
        candidate = create_profile()
        with fake_ai():
            session = engine.start(
                candidate=candidate,
                template=InterviewTemplate.objects.get(slug="general-practice"),
                actor=candidate.user,
                job=self.job,
            )
            turn = engine.next_question(session=session)
        return session, turn

    def assert_no_kit_and_a_working_session(self, template):
        self.assertIsNone(template)
        self.assertFalse(
            InterviewTemplate.objects.filter(slug=planning.slug_for(self.job)).exists()
        )
        # The candidate is not stranded: practice opens on the job-aware
        # fallback — questions about this job, not the general four — and asks
        # a question.
        with fake_ai():
            pools, _, tailored = engine._plan(
                template=InterviewTemplate.objects.get(slug="general-practice"),
                job=self.job,
                candidate=create_profile(),
            )
        self.assertFalse(tailored)
        self.assertTrue(any(key.startswith("role_") for key in pools))
        self.assertIn(
            self.job.title, " ".join(q for pool in pools.values() for q in pool["questions"])
        )
        session, turn = self.start_session()
        self.assertIsNotNone(turn)
        self.assertTrue(turn.question_text)

    def test_invalid_json_twice_leaves_the_fallback(self):
        template = self.plan("not json at all", "{ still not")
        self.assertEqual(AIRun.objects.filter(task_key="interview.plan").count(), 2)
        self.assert_no_kit_and_a_working_session(template)

    def test_sessions_started_while_the_model_fails_do_not_each_ask_again(self):
        # Every session start queues the kit; a failing model used to be asked
        # twice more per session. Now once per posting per cool-down.
        with fake_ai():
            FakeAdapter.queue("not json", "not json", "not json", "not json")
            self.start_session()
            self.start_session()
        self.assertEqual(AIRun.objects.filter(task_key="interview.plan").count(), 2)

    def test_truncated_json_twice_leaves_the_fallback(self):
        truncated = '{"competencies": [{"key": "billing", "label": "Billing", "why": "", "questions": ["Tell me'
        self.assert_no_kit_and_a_working_session(self.plan(truncated, truncated))

    def test_missing_questions_is_refused(self):
        broken = {"competencies": [{"key": "a", "label": "A", "why": ""}] * 2}
        self.assert_no_kit_and_a_working_session(self.plan(broken, broken))

    def test_wrong_types_are_refused(self):
        broken = {"competencies": [_competency("a", [1, 2, 3]), _competency("b", [4, 5, 6])]}
        self.assert_no_kit_and_a_working_session(self.plan(broken, broken))

    def test_a_failure_is_not_followed_by_a_third_attempt(self):
        self.plan("not json", "not json", GOOD)
        self.assertEqual(AIRun.objects.filter(task_key="interview.plan").count(), 2)

    def test_a_retry_that_succeeds_is_used(self):
        template = self.plan("not json", GOOD)
        self.assertIsNotNone(template)
        self.assertEqual(sorted(template.question_pools), ["billing", "delivery"])

    def test_extra_fields_are_ignored(self):
        extra = {
            **GOOD,
            "notes": "ignore me",
            "competencies": [{**row, "difficulty": "hard"} for row in GOOD["competencies"]],
        }
        template = self.plan(extra)
        self.assertIsNotNone(template)
        self.assertNotIn("difficulty", template.question_pools["billing"])

    def test_a_pool_of_blank_questions_is_not_stored(self):
        blank = {
            "competencies": [_competency("a", ["", " ", "  "]), _competency("b", ["", "", ""])]
        }
        self.assert_no_kit_and_a_working_session(self.plan(blank))

    def test_a_pool_of_the_same_question_is_served_once(self):
        same = "Tell me about a billing incident you handled."
        template = self.plan(
            {"competencies": [_competency("a", [same] * 3), GOOD["competencies"][1]]}
        )
        pools = template.question_pools if template else {}
        self.assertEqual(pools.get("a", {}).get("questions", []), [same])

    def test_a_pool_of_hypotheticals_is_not_stored(self):
        hypothetical = [
            "How would you design a billing service?",
            "What would you do if billing failed?",
            "Imagine a billing outage. What would you do?",
        ]
        self.assert_no_kit_and_a_working_session(
            self.plan(
                {"competencies": [_competency("a", hypothetical), _competency("b", hypothetical)]}
            )
        )


class EmployerThingsTests(TestCase):
    """What the posting names as the employer's own, and how questions say it."""

    POSTING = relevance.Posting(
        title="UI/UX Designer",
        description=(
            "Own the experience of our guest-facing booking app, from research with hotel "
            "staff to the component library the engineers build from."
        ),
        duties=["Maintain the product design system", "Run usability research with the team"],
    )

    def test_the_employers_own_things_are_found(self):
        things = planning.employer_things(self.POSTING)
        self.assertIn("guest-facing booking app", things)
        self.assertIn("component library", things)
        self.assertIn("product design system", things)
        # One word is not a thing anybody claims to have worked on.
        self.assertNotIn("team", things)

    def test_a_question_about_the_employers_product_becomes_about_one_like_it(self):
        things = planning.employer_things(self.POSTING)
        question = (
            "Tell me about research you ran to improve the guest-facing booking app. "
            "The product design system: what did you change?"
        )
        self.assertEqual(
            planning.indefinite(question, things),
            "Tell me about research you ran to improve a guest-facing booking app. "
            "A product design system: what did you change?",
        )

    def test_a_question_without_the_employers_things_is_untouched(self):
        question = "Tell me about the last usability test you ran."
        self.assertEqual(
            planning.indefinite(question, planning.employer_things(self.POSTING)), question
        )

    def test_a_stored_kit_is_rewritten_when_it_is_read(self):
        from apps.ai.services import prompts

        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()
        FakeAdapter.reset()
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        job = publish(
            create_job(
                company,
                title="Backend Developer",
                description="You will work on the Django service that takes every reservation.",
            )
        )
        with fake_ai():
            FakeAdapter.queue(
                {
                    "competencies": [
                        _competency(
                            "api",
                            [
                                "Tell me about a change you made to the Django service at your last job.",
                                "Describe a Django service outage you fixed.",
                                "Tell me about a Django service you designed.",
                            ],
                        ),
                        _competency(
                            "data",
                            [
                                "Tell me about a Django migration you ran on live data.",
                                "Describe a Django query you made faster.",
                                "Tell me about a Django reservation bug you fixed.",
                            ],
                        ),
                    ]
                }
            )
            planning.plan_job(job=job)

        served = [
            q for pool in (planning.cached_pools(job) or {}).values() for q in pool["questions"]
        ]
        self.assertIn(
            "Tell me about a change you made to a Django service at your last job.", served
        )
        self.assertFalse(any("the Django service" in q for q in served))
