"""
The AI text interview (FR-10.1 to FR-10.4, SRS §11.2).

The properties this file is answerable for:

  * **practice is private by construction** — no application, so no employer
    queryset reaches it (FR-10.2);
  * **questions are sampled and rotated**, so two sessions do not ask the same
    thing in the same order (FR-10.3);
  * follow-ups are bounded, reference the candidate's own answer, and their
    absence never stops the interview;
  * **`integrity_meta` is recorded and never scored** (FR-10.4);
  * the interview runs with AI off — the pools are the template's, and only the
    prose and the scoring are missing.

Everything runs against `FakeAdapter` through the real P11 runner. Nothing
patches `runner.run`: the point is that this feature uses the shared pipeline.
"""

from __future__ import annotations

import datetime as dt
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.ai.models import AIRun
from apps.ai.providers.base import ProviderFailure
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotFound
from apps.companies.tests.factories import company_with_owner
from apps.core import features
from apps.core.models import FeatureFlag
from apps.interviews.models import (
    InterviewSession,
    QuestionOrigin,
    SessionState,
)
from apps.interviews.services import engine, planning
from apps.interviews.tests.factories import (
    FOLLOWUP_OUTPUT,
    PLAN_OUTPUT,
    fake_ai,
    practice_template,
)
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for


class EngineFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        self.profile = create_profile()
        self.template = practice_template()

    def start(self, **kwargs) -> InterviewSession:
        with fake_ai():
            return engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                **kwargs,
            )

    def answer_everything(
        self, session: InterviewSession, *, text: str = "I did the thing."
    ) -> int:
        """Answer until the interview runs out of questions."""
        answered = 0
        while True:
            with fake_ai():
                FakeAdapter.queue(FOLLOWUP_OUTPUT)
                turn = engine.next_question(session=session)
            if turn is None:
                return answered
            engine.submit_answer(session=session, turn=turn, text=text)
            answered += 1


class StartTests(EngineFixture):
    def test_a_practice_session_has_no_application(self):
        # The whole privacy mechanism (FR-10.2): no application means no
        # employer queryset can reach it.
        session = self.start()
        self.assertIsNone(session.application_id)
        self.assertTrue(session.is_practice)

    def test_a_session_starts_in_progress(self):
        session = self.start()
        self.assertEqual(session.state, SessionState.IN_PROGRESS)
        self.assertIsNotNone(session.started_at)

    def test_the_plan_carries_the_templates_competencies(self):
        session = self.start()
        keys = {row["key"] for row in session.plan["competencies"]}
        self.assertEqual(keys, {"problem_solving", "ownership"})

    def test_the_plan_samples_rather_than_taking_every_question(self):
        # FR-10.3: a fixed bank asked in a fixed order is on a forum within a
        # month, and every candidate after that is answering questions they
        # have already read.
        session = self.start()
        for competency in session.plan["competencies"]:
            with self.subTest(competency=competency["key"]):
                self.assertEqual(len(competency["questions"]), engine.QUESTIONS_PER_COMPETENCY)

    def test_two_sessions_do_not_reliably_ask_the_same_questions(self):
        # Sampling, asserted over enough runs that a coincidence is implausible
        # rather than by asserting one draw differs from another.
        # Eight practice sessions in a month is a Pro allowance (ADR-A59).
        from apps.billing.tests.factories import put_on_pro

        put_on_pro(self.profile)
        seen = set()
        for _ in range(8):
            session = self.start()
            for competency in session.plan["competencies"]:
                seen.update(competency["questions"])
        self.assertGreater(len(seen), 2 * engine.QUESTIONS_PER_COMPETENCY)

    def test_a_template_with_no_questions_is_refused(self):
        empty = practice_template(slug="empty", question_pools={})
        with fake_ai(), self.assertRaises(InvalidState):
            engine.start(candidate=self.profile, template=empty, actor=self.profile.user)

    def test_another_candidates_application_is_refused(self):
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        job = publish(create_job(company))
        stranger = create_profile()
        application = application_for(stranger, job)

        with fake_ai(), self.assertRaises(InvalidState):
            engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                application=application,
            )

    def test_starting_is_audited(self):
        from apps.audit.models import AuditLog

        self.start()
        self.assertTrue(AuditLog.objects.filter(verb="interview.started").exists())


class PlanningTests(EngineFixture):
    """Which pool an interview runs on, and what the candidate waits for.

    The rule that shapes all of this: **nothing on the request path calls a
    model.** Planning a role's interview is the largest generation this product
    makes — a hundred seconds on a modest model — and it used to happen while
    the candidate waited for Start. It timed out, every session silently fell
    back to the four generic competencies, and the feature looked implemented
    while doing nothing.

    So a session opens on the best pool that exists at that moment, and the
    role's own kit is written off the request by `tasks.plan_job_interview`.
    """

    def test_starting_an_interview_never_waits_for_a_model(self):
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        job = publish(create_job(company))

        with fake_ai(), mock.patch("apps.interviews.tasks.plan_job_interview.delay") as queued:
            engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user, job=job
            )

        self.assertFalse(AIRun.objects.filter(task_key="interview.plan").exists())
        queued.assert_called_once_with(job.pk)

    def test_a_job_with_a_written_kit_is_interviewed_from_it(self):
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        job = publish(create_job(company))

        # A kit whose questions name the posting ("own the billing service").
        # A generic kit is now rewritten in the background by design; that
        # case is `test_ai_defects.AnchoringTests`. What this test holds is
        # that a good kit is used and nothing is regenerated. Three distinct
        # questions per competency: a pool of two is thin and is rewritten too.
        anchored = {
            "competencies": [
                {
                    **item,
                    "questions": [
                        f"Tell me about a billing service change you shipped ({n}).",
                        f"Describe a billing incident you worked on ({n}).",
                        f"Walk me through an invoice error you traced to its cause in billing ({n}).",
                    ],
                }
                for n, item in enumerate(PLAN_OUTPUT["competencies"])
            ]
        }
        with fake_ai():
            FakeAdapter.queue(anchored)
            self.assertIsNotNone(planning.plan_job(job=job))

        with fake_ai(), mock.patch("apps.interviews.tasks.plan_job_interview.delay") as queued:
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user, job=job
            )

        keys = {item["key"] for item in session.plan["competencies"]}
        self.assertEqual(keys, {item["key"] for item in PLAN_OUTPUT["competencies"]})
        queued.assert_not_called()

    def test_a_kit_that_has_not_been_written_yet_falls_back_rather_than_refusing(self):
        # The first person to practise for a new posting still gets an
        # interview. They get the general pool; everybody after them gets the
        # role's own.
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        job = publish(create_job(company))

        with fake_ai(), mock.patch("apps.interviews.tasks.plan_job_interview.delay"):
            session = engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user, job=job
            )

        self.assertTrue(session.plan["competencies"])

    def test_a_templates_own_pools_are_not_second_guessed_by_a_model(self):
        # A company that wrote its competency framework should not have a model
        # overrule it.
        self.start()
        self.assertFalse(AIRun.objects.filter(task_key="interview.plan").exists())

    def test_a_template_with_no_questions_and_no_job_is_refused(self):
        bare = practice_template(slug="bare", question_pools={})

        with self.assertRaises(InvalidState):
            engine.start(candidate=self.profile, template=bare, actor=self.profile.user)


class KitQualityTests(TestCase):
    """What reaches a candidate, whichever model wrote the kit."""

    def test_a_hypothetical_question_never_reaches_the_pool(self):
        """ "How would you debug a system that is not processing transactions?"

        Gets the answer the candidate rehearsed on the bus, which is the
        opposite of what a structured interview is for. The prompt bans these
        and the models write them anyway.
        """
        pools = planning._pools_from(
            {
                "competencies": [
                    {
                        "key": "problem_solving",
                        "label": "Problem solving",
                        "why": "Predicts how they handle an incident.",
                        "questions": [
                            "How would you debug a ledger that did not balance?",
                            "Tell me about a time two systems disagreed about the same number.",
                            "What is your definition of idempotency?",
                        ],
                    }
                ]
            }
        )

        self.assertEqual(
            pools["problem_solving"]["questions"],
            ["Tell me about a time two systems disagreed about the same number."],
        )

    def test_questions_that_do_not_ask_for_real_work_never_reach_the_pool(self):
        """Every shape measured in the 26 kits the local model actually wrote.

        The first filter matched nine literal openings and let 14% of real
        questions through: "walk me through how you would...", "how do you
        ensure...", "what steps do you take to...". Each of these gets the
        textbook answer rather than an account of something the candidate did.
        """
        rejected = [
            "Walk me through how you would set up a Django application to use PostgreSQL.",
            "Describe how you would design a CI/CD pipeline for a new microservice.",
            "How do you ensure the security of data stored in AWS S3?",
            "What steps do you take to ensure the maintainability of a mobile codebase?",
            "What strategies do you use to ensure your communication is clear and concise?",
            "What methods do you use to prepare materials before a recording session?",
            "Walk me through how you ensure the code you write adheres to best practices.",
            "Imagine a payout endpoint fails its retries. What do you do first?",
            "What is the difference between a ledger and a journal?",
        ]
        kept = [
            "Tell me about a time two systems disagreed about the same number.",
            "What steps did you take to ensure the new warehouse system handled the load?",
            "Walk me through how you resolved an issue where two ledgers disagreed.",
            "How did you measure the success of a brand campaign you ran?",
            # A habitual opening rescued by a request for a real example.
            "How do you handle large datasets in Python? Can you give an example of a project?",
            "How do you ensure payout endpoints are safe to retry? Walk me through a specific example.",
        ]

        for question in rejected:
            with self.subTest(rejected=question):
                self.assertTrue(planning._is_hypothetical(question))
        for question in kept:
            with self.subTest(kept=question):
                self.assertFalse(planning._is_hypothetical(question))

    def test_a_kit_written_before_the_filter_is_filtered_when_read(self):
        """Cached kits live ninety days; a tightened rule must reach them too."""
        from apps.interviews.models import InterviewTemplate

        owner = create_profile().user
        company, _ = company_with_owner(owner)
        job = publish(create_job(company))
        InterviewTemplate.objects.create(
            company=company,
            slug=planning.slug_for(job),
            name="old kit",
            kind="ai_text",
            question_pools={
                "delivery": {
                    "label": "Delivery",
                    "why": "",
                    "questions": [
                        "How do you ensure a release is safe?",
                        "Tell me about a release you rolled back.",
                    ],
                }
            },
            config={"source": planning.AUTO_SOURCE, "source_sha256": planning._digest(job)},
        )

        pools = planning.cached_pools(job)

        self.assertEqual(
            pools["delivery"]["questions"], ["Tell me about a release you rolled back."]
        )

    def test_a_junior_role_is_not_asked_about_managing_people(self):
        output = {
            "competencies": [
                {
                    "key": "teamwork",
                    "label": "Teamwork",
                    "why": "",
                    "questions": [
                        "Walk me through a situation where you had to delegate tasks to a team member.",
                        "Tell me about a time you gave feedback during a performance review.",
                        "Tell me about a group project where the plan changed halfway.",
                    ],
                }
            ]
        }

        junior = planning._pools_from(output, junior=True)
        senior = planning._pools_from(output, junior=False)

        self.assertEqual(
            junior["teamwork"]["questions"],
            ["Tell me about a group project where the plan changed halfway."],
        )
        self.assertEqual(len(senior["teamwork"]["questions"]), 3)

    def test_a_competency_left_with_no_questions_is_dropped(self):
        pools = planning._pools_from(
            {
                "competencies": [
                    {
                        "key": "hypotheticals_only",
                        "label": "Nothing usable",
                        "why": "",
                        "questions": ["How would you design a queue?"],
                    }
                ]
            }
        )

        self.assertEqual(pools, {})


class InterviewKitTests(EngineFixture):
    """`services.planning` — one kit per posting, reused."""

    def setUp(self):
        super().setUp()
        owner = create_profile().user
        self.company, _ = company_with_owner(owner)
        self.job = publish(create_job(self.company))

    def _write(self):
        with fake_ai():
            FakeAdapter.queue(PLAN_OUTPUT)
            return planning.plan_job(job=self.job)

    def test_a_kit_is_written_once_and_read_back(self):
        self._write()

        self.assertIsNotNone(planning.cached_pools(self.job))

    def test_the_kit_is_stored_as_a_template_the_employer_can_edit(self):
        template = self._write()

        self.assertEqual(template.company, self.company)
        self.assertTrue(template.is_active)
        self.assertEqual(template.config["source"], planning.AUTO_SOURCE)

    def test_rewriting_the_advert_invalidates_the_kit(self):
        # The kit was written from the posting's own words. Change them and it
        # no longer describes the role it is used to interview for.
        self._write()
        self.job.description = "An entirely different job, in a different field."
        self.job.save(update_fields=["description"])

        self.assertIsNone(planning.cached_pools(self.job))

    def test_a_recruiters_own_edits_are_never_regenerated_over(self):
        template = self._write()
        template.config = {"source": "recruiter"}
        template.question_pools = {
            "theirs": {"label": "Theirs", "why": "", "questions": ["A question they wrote."]}
        }
        template.save(update_fields=["config", "question_pools"])

        with fake_ai():
            FakeAdapter.queue(PLAN_OUTPUT)
            planning.plan_job(job=self.job)

        template.refresh_from_db()
        self.assertIn("theirs", template.question_pools)

    def test_a_model_that_is_down_leaves_no_kit_rather_than_an_empty_one(self):
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            self.assertIsNone(planning.plan_job(job=self.job))

        self.assertIsNone(planning.cached_pools(self.job))


class TurnTests(EngineFixture):
    def test_the_first_question_comes_from_the_pool(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        self.assertIsNotNone(turn)
        self.assertEqual(turn.question_origin, QuestionOrigin.POOL)
        self.assertEqual(turn.order, 0)

    def test_asking_twice_returns_the_same_unanswered_question(self):
        # A refresh must not skip a question.
        session = self.start()
        with fake_ai():
            first = engine.next_question(session=session)
            second = engine.next_question(session=session)
        self.assertEqual(first.pk, second.pk)

    def test_an_answer_is_recorded_with_its_timing(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        engine.submit_answer(
            session=session, turn=turn, text="I rewrote the importer.", duration_ms=45_000
        )

        turn.refresh_from_db()
        self.assertEqual(turn.answer_text, "I rewrote the importer.")
        self.assertEqual(turn.duration_ms, 45_000)
        self.assertIsNotNone(turn.answered_at)

    def test_an_empty_answer_is_refused(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        with self.assertRaises(InvalidState):
            engine.submit_answer(session=session, turn=turn, text="   ")

    def test_answering_twice_is_refused(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=turn, text="Once.")

        with self.assertRaises(InvalidState):
            engine.submit_answer(session=session, turn=turn, text="Twice.")

    def test_a_turn_from_another_session_is_not_found(self):
        first = self.start()
        second = self.start()
        with fake_ai():
            turn = engine.next_question(session=first)

        with self.assertRaises(NotFound):
            engine.submit_answer(session=second, turn=turn, text="Wrong session.")

    def test_a_question_can_be_skipped(self):
        # A candidate stuck on question three either skips it or abandons, and
        # an abandoned interview tells the employer nothing about the other nine.
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        engine.skip(session=session, turn=turn)
        turn.refresh_from_db()
        self.assertEqual(turn.answer_text, "(skipped)")

    def test_an_answer_is_capped(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        engine.submit_answer(session=session, turn=turn, text="x" * 50_000)
        turn.refresh_from_db()
        self.assertEqual(len(turn.answer_text), engine.MAX_ANSWER_CHARS)

    def test_a_finished_session_takes_no_more_answers(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)
        engine.abandon(session=session)

        with self.assertRaises(InvalidState):
            engine.submit_answer(session=session, turn=turn, text="Too late.")

    def test_every_planned_question_is_eventually_asked(self):
        session = self.start()
        answered = self.answer_everything(session)

        planned = sum(len(row["questions"]) for row in session.plan["competencies"])
        self.assertGreaterEqual(answered, planned)


class IntegrityMetaTests(EngineFixture):
    """FR-10.4. Recorded as context, never as a score input."""

    def test_paste_and_focus_events_are_recorded(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        engine.submit_answer(
            session=session,
            turn=turn,
            text="I did it.",
            integrity={"pastes": 3, "blur_events": 2, "keystrokes": 400},
        )

        turn.refresh_from_db()
        self.assertEqual(turn.integrity_meta["pastes"], 3)
        self.assertEqual(turn.integrity_meta["blur_events"], 2)

    def test_only_counts_are_kept(self):
        # Client-supplied and rendered on an employer's screen: an allow-list,
        # or it is a stored XSS vector wearing a security feature's name.
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        engine.submit_answer(
            session=session,
            turn=turn,
            text="I did it.",
            integrity={"pastes": 1, "note": "<script>alert(1)</script>"},
        )

        turn.refresh_from_db()
        self.assertNotIn("note", turn.integrity_meta)
        self.assertEqual(set(turn.integrity_meta), {"pastes", "blur_events", "keystrokes"})

    def test_negative_counts_are_floored(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        engine.submit_answer(session=session, turn=turn, text="I did it.", integrity={"pastes": -5})
        turn.refresh_from_db()
        self.assertEqual(turn.integrity_meta["pastes"], 0)

    def test_running_over_the_time_box_is_reported_not_penalised(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)

        engine.submit_answer(
            session=session,
            turn=turn,
            text="I did it.",
            duration_ms=(engine.ANSWER_SECONDS + 60) * 1000,
        )
        turn.refresh_from_db()
        self.assertTrue(turn.ran_over)


class FollowupTests(EngineFixture):
    def test_a_followup_is_asked_after_the_planned_questions(self):
        session = self.start()

        # Answer every planned question for the first competency.
        first = session.plan["competencies"][0]["key"]
        for _ in range(engine.QUESTIONS_PER_COMPETENCY):
            with fake_ai():
                turn = engine.next_question(session=session)
            engine.submit_answer(session=session, turn=turn, text="I rebuilt the importer.")

        with fake_ai():
            FakeAdapter.queue(FOLLOWUP_OUTPUT)
            turn = engine.next_question(session=session)

        self.assertEqual(turn.question_origin, QuestionOrigin.AI_FOLLOWUP)
        self.assertEqual(turn.competency, first)

    def test_a_followup_is_given_the_candidates_own_answer(self):
        # §11.2: hard to fake cold, which is the whole anti-cheat framing.
        session = self.start()
        for _ in range(engine.QUESTIONS_PER_COMPETENCY):
            with fake_ai():
                turn = engine.next_question(session=session)
            engine.submit_answer(session=session, turn=turn, text="UNIQUE-ANSWER-MARKER")

        with fake_ai():
            FakeAdapter.queue(FOLLOWUP_OUTPUT)
            engine.next_question(session=session)

        self.assertIn("UNIQUE-ANSWER-MARKER", FakeAdapter.calls[-1]["user"])

    def test_a_followup_that_restates_an_earlier_question_is_dropped(self):
        """Being asked the same thing twice is what makes an interview bad.

        The model read its own follow-up and asked it again in other words:
        "What steps did you take after realising you missed the deadline to
        ensure it wouldn't happen again?" then "Can you walk me through the
        steps you took immediately after missing the deadline to prevent it
        from happening again?"
        """
        session = self.start()
        for _ in range(engine.QUESTIONS_PER_COMPETENCY):
            with fake_ai():
                turn = engine.next_question(session=session)
            engine.submit_answer(session=session, turn=turn, text="I rebuilt the importer.")

        with fake_ai():
            FakeAdapter.queue(
                {
                    "question": (
                        "What steps did you take after realising you missed the "
                        "deadline to ensure it would not happen again?"
                    )
                }
            )
            first = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=first, text="I told them the same day.")

        with fake_ai():
            FakeAdapter.queue(
                {
                    "question": (
                        "Can you walk me through the steps you took immediately "
                        "after missing the deadline to prevent it happening again?"
                    )
                }
            )
            second = engine.next_question(session=session)

        self.assertNotEqual(second.question_origin, QuestionOrigin.AI_FOLLOWUP)

    def test_a_genuinely_new_followup_is_still_asked(self):
        session = self.start()
        for _ in range(engine.QUESTIONS_PER_COMPETENCY):
            with fake_ai():
                turn = engine.next_question(session=session)
            engine.submit_answer(session=session, turn=turn, text="I rebuilt the importer.")

        with fake_ai():
            FakeAdapter.queue({"question": "Which part of the importer was slowest, and why?"})
            turn = engine.next_question(session=session)

        self.assertEqual(turn.question_origin, QuestionOrigin.AI_FOLLOWUP)

    def test_followups_are_bounded_per_competency(self):
        session = self.start()
        for _ in range(20):
            with fake_ai():
                FakeAdapter.queue(FOLLOWUP_OUTPUT)
                turn = engine.next_question(session=session)
            if turn is None:
                break
            engine.submit_answer(session=session, turn=turn, text="An answer.")

        for competency in session.plan["competencies"]:
            with self.subTest(competency=competency["key"]):
                self.assertLessEqual(
                    session.turns.filter(
                        competency=competency["key"],
                        question_origin=QuestionOrigin.AI_FOLLOWUP,
                    ).count(),
                    engine.MAX_FOLLOWUPS_PER_COMPETENCY,
                )

    def test_a_failed_followup_does_not_stop_the_interview(self):
        # A follow-up is an improvement to the interview, not a requirement.
        session = self.start()
        for _ in range(engine.QUESTIONS_PER_COMPETENCY):
            with fake_ai():
                turn = engine.next_question(session=session)
            engine.submit_answer(session=session, turn=turn, text="An answer.")

        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            turn = engine.next_question(session=session)

        self.assertIsNotNone(turn)
        self.assertEqual(turn.question_origin, QuestionOrigin.POOL)

    def test_a_skipped_answer_gets_no_followup(self):
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)
        engine.skip(session=session, turn=turn)

        with fake_ai():
            FakeAdapter.queue(FOLLOWUP_OUTPUT)
            nxt = engine.next_question(session=session)

        self.assertEqual(nxt.question_origin, QuestionOrigin.POOL)

    def test_the_total_turn_count_is_capped(self):
        session = self.start()
        for _ in range(engine.MAX_TURNS + 10):
            with fake_ai():
                FakeAdapter.queue(FOLLOWUP_OUTPUT)
                turn = engine.next_question(session=session)
            if turn is None:
                break
            engine.submit_answer(session=session, turn=turn, text="An answer.")

        self.assertLessEqual(session.turns.count(), engine.MAX_TURNS)


class ConcludeTests(EngineFixture):
    def test_concluding_closes_the_session(self):
        session = self.start()
        self.answer_everything(session)

        with fake_ai():
            engine.conclude(session=session)

        session.refresh_from_db()
        self.assertEqual(session.state, SessionState.COMPLETED)
        self.assertIsNotNone(session.ended_at)

    def test_concluding_emits_counts_and_not_answers(self):
        from apps.notifications.models import OutboxEvent

        session = self.start()
        self.answer_everything(session, text="SECRET-ANSWER-TEXT")
        with fake_ai():
            engine.conclude(session=session)

        event = OutboxEvent.objects.filter(type="interview.completed").first()
        self.assertIsNotNone(event)
        self.assertNotIn("SECRET-ANSWER-TEXT", str(event.payload))
        self.assertGreater(event.payload["turns"], 0)

    def test_concluding_queues_evaluation_rather_than_running_it(self):
        """The candidate never waits for scoring.

        `captureOnCommitCallbacks` because the queueing is deliberately on
        commit: a worker that picked the session up before the transaction
        landed would read a session that does not exist yet, or one that was
        about to be rolled back.
        """
        session = self.start()
        self.answer_everything(session)

        with (
            mock.patch("apps.interviews.tasks.evaluate_session.delay") as queued,
            self.captureOnCommitCallbacks(execute=True),
            fake_ai(),
        ):
            engine.conclude(session=session)
        queued.assert_called_once_with(session.pk)

    def test_concluding_twice_changes_nothing(self):
        session = self.start()
        self.answer_everything(session)
        with fake_ai():
            engine.conclude(session=session)
            ended = InterviewSession.objects.get(pk=session.pk).ended_at
            engine.conclude(session=session)

        self.assertEqual(InterviewSession.objects.get(pk=session.pk).ended_at, ended)

    def test_abandoning_produces_no_outcome(self):
        # Half an interview is not a weak interview.
        session = self.start()
        with fake_ai():
            turn = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=turn, text="One answer.")

        engine.abandon(session=session, reason="Closed the tab")
        session.refresh_from_db()

        self.assertEqual(session.state, SessionState.ABANDONED)
        self.assertIsNone(engine.outcome_for(session))


class DegradedTests(EngineFixture):
    """The interview with AI unavailable (§9.3.6)."""

    def test_an_interview_runs_with_ai_switched_off(self):
        FeatureFlag.objects.create(
            key="ai",
            is_enabled=False,
            expires_on=timezone.localdate() + dt.timedelta(days=7),
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

        session = self.start()
        answered = self.answer_everything(session)

        self.assertGreater(answered, 0)
        self.assertEqual(len(FakeAdapter.calls), 0)

    def test_no_followups_are_asked_with_ai_off(self):
        FeatureFlag.objects.create(
            key="ai",
            is_enabled=False,
            expires_on=timezone.localdate() + dt.timedelta(days=7),
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

        session = self.start()
        self.answer_everything(session)

        self.assertFalse(session.turns.filter(question_origin=QuestionOrigin.AI_FOLLOWUP).exists())


class RecommendationTests(TestCase):
    def test_the_bands_are_ordered_and_exhaustive(self):
        self.assertEqual(engine.recommend(5.0), "strong_yes")
        self.assertEqual(engine.recommend(4.3), "strong_yes")
        self.assertEqual(engine.recommend(3.5), "yes")
        self.assertEqual(engine.recommend(3.0), "no")
        self.assertEqual(engine.recommend(2.2), "no")
        self.assertEqual(engine.recommend(1.0), "strong_no")
