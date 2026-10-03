"""
Evidence before drafting, and gaps from a target job (ASC-01, ENH-01).

Structural assertions throughout: which questions are asked and when, what
reaches the model (the rendered prompt, as the fake provider received it) and
what reaches the CV — never one generated sentence.
"""

from __future__ import annotations

from django.urls import reverse

from apps.ai.models import RunStatus
from apps.ai.providers.fake import FakeAdapter
from apps.billing.tests import factories as billing_factories
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import JobSkill, Requirement
from apps.jobs.tests.factories import create_job, create_skill, publish
from apps.resumes import selectors
from apps.resumes.models import BuilderMode, ResumeSuggestion
from apps.resumes.services import analysis as analysis_service
from apps.resumes.services import assistant as assistant_service
from apps.resumes.services import builders as builder_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import evidence
from apps.taxonomy.models import Skill

from .test_assistant import REPLY_OUTPUT, AssistantFixture


class ProbeRuleTests(AssistantFixture):
    def test_a_bare_skill_claim_is_recognised(self):
        for said, skill in (
            ("I have Python skills.", "Python"),
            ("I know Django", "Django"),
            ("I have some Excel experience", "Excel"),
            ("I'm familiar with Figma.", "Figma"),
        ):
            with self.subTest(said=said):
                self.assertEqual(evidence.claimed_skill(said), skill)

    def test_a_description_with_context_is_not_probed(self):
        step = evidence.next_step(
            {},
            "At Nexa Payments I built the settlement service in Python, "
            "which cut the run from 4 hours to 35 minutes.",
        )

        self.assertEqual(step.question, "")

    def test_where_then_what_then_result_then_enough(self):
        state: dict = {}
        asked = []
        for said in (
            "I have Python skills.",
            "At my internship at Phewa Stays.",
            "I built the booking API.",
            "It is used by all four guesthouses now.",
        ):
            step = evidence.next_step(state, said)
            state = step.state or {}
            asked.append(step.question)

        self.assertIn("Where have you used Python", asked[0])
        self.assertIn("What did you build", asked[1])
        self.assertIn("What came of it", asked[2])
        self.assertEqual(asked[3], "")
        self.assertIn("Phewa Stays", step.latest)
        self.assertIn("booking API", step.latest)

    def test_only_the_missing_piece_is_asked(self):
        # Where and what are both there already: only the result is missing.
        step = evidence.next_step({}, "I know SQL. At Phewa Stays I built the reports.")

        self.assertIn("What came of it", step.question)

    def test_the_candidate_can_stop_the_probe(self):
        step = evidence.next_step({}, "I know SQL")
        step = evidence.next_step(step.state, "skip")

        self.assertEqual(step.question, "")
        self.assertNotIn(evidence.PROBE_KEY, step.state)


class AssistantProbeTests(AssistantFixture):
    def test_a_skill_only_answer_is_followed_up_without_a_model_call(self):
        reply = self.respond(text="I have Python skills.", output=None)

        self.assertIn("Where have you used Python", reply.text)
        self.assertEqual(reply.run.status, RunStatus.NOT_ASKED)
        self.assertEqual(FakeAdapter.calls, [])

    def test_a_skill_claim_writes_nothing_to_the_cv(self):
        before = dict(self.document.draft.content)

        self.respond(text="I have Python skills.", output=None)
        self.document.draft.refresh_from_db()

        self.assertEqual(self.document.draft.content, before)
        self.assertFalse(ResumeSuggestion.objects.filter(draft=self.document.draft).exists())

    def test_after_the_probe_the_model_reads_the_whole_account(self):
        self.respond(text="I know Celery.", output=None)
        self.respond(text="At Nexa Payments.", output=None)
        self.respond(text="I moved 12 batch jobs onto Celery.", output=None)

        reply = self.respond(
            text="It cut the nightly settlement run from 4 hours to 35 minutes.",
            output=REPLY_OUTPUT,
        )

        self.assertTrue(reply.ok)
        prompt = FakeAdapter.calls[-1]["user"]
        self.assertIn("Nexa Payments", prompt)
        self.assertIn("12 batch jobs", prompt)
        # Proposals, not edits: accepting is still the only way into the CV.
        self.assertTrue(ResumeSuggestion.objects.filter(draft=self.document.draft).exists())


class TargetJobFixture(AssistantFixture):
    def setUp(self):
        super().setUp()
        billing_factories.put_on_pro(self.profile)
        company, _ = company_with_owner(create_profile().user, name="Summit Digital Labs")
        self.job = create_job(company, title="Backend Developer")
        for order, name in enumerate(["Python", "Django", "Kubernetes"]):
            skill = Skill.objects.filter(name=name).first() or create_skill(name)
            JobSkill.objects.create(
                job=self.job, skill=skill, requirement=Requirement.REQUIRED, order=order
            )
        publish(self.job)
        draft_service.set_contact(draft=self.document.draft, field="name", value="Maya Karki")
        self.document.refresh_from_db()


class TargetJobTests(TargetJobFixture):
    def test_setting_a_target_opens_with_the_first_gap(self):
        question = assistant_service.set_target(
            document=self.document, title="Backend Developer", missing=["Django", "Kubernetes"]
        )

        self.assertIn("Backend Developer role asks for Django", question)
        session = builder_service.session_for(document=self.document, mode=BuilderMode.ASSISTANT)
        self.assertEqual(session.transcript[-1]["text"], question)

    def test_the_target_and_its_gaps_reach_the_model(self):
        assistant_service.set_target(
            document=self.document, title="Backend Developer", missing=["Kubernetes"]
        )

        self.respond(output=REPLY_OUTPUT)

        prompt = FakeAdapter.calls[-1]["user"]
        self.assertIn("Backend Developer", prompt)
        self.assertIn("Kubernetes", prompt)
        self.assertIn("never assume they have it", prompt)

    def test_gaps_are_asked_one_at_a_time_and_a_covered_one_is_skipped(self):
        state = evidence.set_target({}, title="Backend Developer", missing=["Django", "Celery"])

        first, state = evidence.gap_question(state, cv_text="Python, Celery")
        second, state = evidence.gap_question(state, cv_text="Python, Celery")

        self.assertIn("Django", first)
        self.assertEqual(second, "")

    def test_a_no_to_a_gap_adds_nothing(self):
        assistant_service.set_target(
            document=self.document, title="Backend Developer", missing=["Kubernetes"]
        )
        before = dict(self.document.draft.content)

        self.respond(
            text="No, I have not used Kubernetes.",
            output={"reply": "Understood.", "role": REPLY_OUTPUT["role"], "bullets": []},
        )
        self.document.draft.refresh_from_db()

        self.assertEqual(self.document.draft.content, before)
        self.assertFalse(ResumeSuggestion.objects.filter(draft=self.document.draft).exists())

    def test_location_is_asked_only_when_aiming_at_a_job_and_never_filled_in(self):
        content = {"contact": {"location": ""}}

        # Untargeted, the CV's own gaps are asked (ASC-02) — never filled in.
        untargeted, _ = assistant_service._next_question({}, self.document)
        self.assertNotEqual(untargeted, "")

        question, state = evidence.location_question({"target": {"title": "x"}}, content)
        self.assertIn("where are you based", question)
        again, _ = evidence.location_question(state, content)
        self.assertEqual(again, "")


class EnhancementScreenTests(TargetJobFixture):
    """CV → pick a job → gaps → assistant, through the screens."""

    def test_analyse_against_a_job_then_answer_its_gaps(self):
        self.client.force_login(self.actor)

        response = self.client.post(
            reverse("resume:analysis_run"),
            {"document": str(self.document.public_id), "job": str(self.job.public_id)},
            HTTP_HX_REQUEST="true",
        )

        latest = selectors.latest_analysis_for(self.document)
        self.assertEqual(latest.job_id, self.job.pk)
        self.assertIn("Kubernetes", latest.target.get("missing", []))
        self.assertContains(response, "Answer questions about these")

        go = self.client.post(
            reverse("resume:assistant_target", kwargs={"document_id": self.document.public_id}),
            {"analysis": str(latest.public_id)},
        )
        page = self.client.get(go["Location"])

        self.assertContains(page, "Backend Developer role asks for")
        self.assertContains(page, "Working on the gaps")

    def test_enhancement_keeps_every_existing_fact(self):
        before = dict(self.document.draft.content)
        analysis = analysis_service.analyse(
            document=self.document, actor=self.actor, job=self.job, defer_ai=True
        )
        self.client.force_login(self.actor)

        self.client.post(
            reverse("resume:assistant_target", kwargs={"document_id": self.document.public_id}),
            {"analysis": str(analysis.public_id)},
        )
        self.document.draft.refresh_from_db()

        self.assertEqual(self.document.draft.content, before)

    def test_another_candidate_cannot_aim_at_this_analysis(self):
        analysis = analysis_service.analyse(
            document=self.document, actor=self.actor, job=self.job, defer_ai=True
        )
        self.client.force_login(create_profile().user)

        response = self.client.post(
            reverse("resume:assistant_target", kwargs={"document_id": self.document.public_id}),
            {"analysis": str(analysis.public_id)},
        )

        self.assertEqual(response.status_code, 404)


class ProbeCaseTests(TargetJobFixture):
    """The verification cases, A to E (2026-09-21, EVD-01..05).

    Before: "I used Python at Phewa Stays." asked nothing and went straight to
    drafting; "I know Python and Django from my job" was read as a skill called
    "Python and Django from my job at Phewa S"; "for a while" counted as a
    place and "3 years" as a result; and a "no" to a gap question was sent to
    the model, so with the model down the remaining gaps were never asked.
    """

    def setUp(self):
        super().setUp()
        from apps.resumes.analysis import target as target_rules

        target_rules.reset_vocabulary()
        self.addCleanup(target_rules.reset_vocabulary)

    def ask(self, said):
        return evidence.next_step({}, said).question

    def test_case_a_a_bare_claim_is_asked_where(self):
        self.assertIn("Where have you used Python", self.ask("I know Python."))

    def test_case_b_where_is_not_asked_again(self):
        question = self.ask("I used Python at Phewa Stays.")

        self.assertNotIn("Where", question)
        self.assertIn("What did you build or work on with Python", question)

    def test_case_c_only_the_outcome_is_asked(self):
        question = self.ask("I used Python at Phewa Stays to build an internal reporting tool.")

        self.assertIn("What came of it", question)

    def test_case_d_a_complete_account_is_not_questioned(self):
        self.assertEqual(
            self.ask(
                "I used Python at Phewa Stays to build an internal reporting tool "
                "that reduced manual reporting time."
            ),
            "",
        )

    def test_the_skill_is_the_skill_not_the_rest_of_the_sentence(self):
        self.assertEqual(
            evidence.claimed_skill("I know Python and Django from my job at Phewa Stays"),
            "Python and Django",
        )
        self.assertEqual(evidence.claimed_skill("I know Terraform from my job"), "Terraform")

    def test_how_long_is_not_where_or_what_came_of_it(self):
        self.assertIn("Where", self.ask("I have used Python for a while"))
        self.assertIn(
            "What came of it",
            self.ask("I have worked with Python for 3 years at Phewa Stays and built a tool"),
        )

    def test_case_e_every_gap_is_asked_and_none_is_assumed(self):
        question = assistant_service.set_target(
            document=self.document,
            title="Backend Developer",
            missing=["Django", "Docker", "PostgreSQL"],
        )
        self.assertIn("asks for Django", question)

        declined = self.respond(text="No, I have not used Django.", output=None)
        self.assertIn("Django stays off your CV", declined.text)
        self.assertIn("asks for Docker", declined.question)

        probed = self.respond(text="Yes", output=None)
        self.assertIn("Where have you used Docker", probed.text)

        # Nothing about any of the three reached the CV or a suggestion.
        self.document.draft.refresh_from_db()
        text = str(self.document.draft.content)
        for skill in ("Django", "Docker", "PostgreSQL"):
            self.assertNotIn(skill, text)
        self.assertFalse(ResumeSuggestion.objects.filter(draft=self.document.draft).exists())
        # The "no" was answered without a model.
        self.assertEqual(FakeAdapter.calls, [])

    def test_the_last_no_says_the_gaps_are_done(self):
        # Located already, so the location question does not follow the last gap.
        draft_service.set_contact(draft=self.document.draft, field="location", value="Pokhara")
        self.document.refresh_from_db()
        assistant_service.set_target(
            document=self.document, title="Backend Developer", missing=["Kubernetes"]
        )

        reply = self.respond(text="Never used it.", output=None)

        self.assertIn("Kubernetes stays off your CV", reply.text)
        self.assertIn("That is every skill the posting asks for", reply.text)

    def test_with_the_model_down_the_next_gap_is_still_asked(self):
        from apps.ai.providers.base import ProviderFailure

        assistant_service.set_target(
            document=self.document, title="Backend Developer", missing=["Django", "Docker"]
        )

        reply = self.respond(
            text=(
                "Yes, I used Django at Phewa Stays to build the booking API, "
                "which now takes every reservation for 12 hotels."
            ),
            output=ProviderFailure("down"),
        )

        self.assertIn(assistant_service.UNAVAILABLE, reply.text)
        self.assertIn("asks for Docker", reply.text)

    def test_no_problem_is_not_a_no(self):
        self.assertFalse(evidence.answered_no("No problem, I used it at Phewa Stays."))
        self.assertFalse(evidence.answered_no("No, but I used Flask."))
        self.assertTrue(evidence.answered_no("No, never."))

    def test_an_answer_in_words_the_patterns_do_not_know_still_counts(self):
        """EVD-06: "I containerised the booking API" answers "what".

        The probe used to stop there — "what" had been asked, its wording was
        not recognised — and never asked what came of it.
        """
        state = {}
        asked = []
        for said in (
            "I know Docker.",
            "At Phewa Stays, where I work now.",
            "I containerised the booking API and its background worker.",
        ):
            step = evidence.next_step(state, said)
            state = step.state
            asked.append(step.question)

        self.assertIn("Where", asked[0])
        self.assertIn("What did you build", asked[1])
        self.assertIn("What came of it", asked[2])


class GapOrderTests(TargetJobFixture):
    def test_required_gaps_are_asked_before_preferred_ones(self):
        from portals.candidate.resume_views import _gaps_required_first

        target = {
            "missing": ["Design systems", "Figma", "UX research"],
            "missing_required": ["Figma", "UX research"],
        }

        self.assertEqual(_gaps_required_first(target), ["Figma", "UX research", "Design systems"])
        # A report written before the split keeps its order.
        self.assertEqual(_gaps_required_first({"missing": ["B", "A"]}), ["B", "A"])


class CvGapTests(AssistantFixture):
    """ASC-02: the assistant notices what the CV lacks and asks, once each."""

    def test_an_empty_cv_is_asked_about_experience_first_and_nothing_twice(self):
        from apps.resumes import content as content_rules

        content = (
            content_rules.new_document_content()
            if hasattr(content_rules, "new_document_content")
            else {"sections": [], "contact": {}}
        )
        state: dict = {}
        asked = []
        for _ in range(10):
            question, state = evidence.cv_gap_question(state, content)
            if not question:
                break
            asked.append(question)

        self.assertIn("most recent job", asked[0])
        self.assertEqual(len(asked), len(set(asked)))
        joined = " ".join(asked)
        for topic in ("aiming for", "project", "tools or skills", "based"):
            self.assertIn(topic, joined)

    def test_a_sufficient_cv_is_not_questioned(self):
        from apps.resumes import examples

        content = examples.example_content(name="Maya Karki", email="maya@example.test")
        content["contact"]["location"] = "Pokhara"
        content["contact"]["headline"] = "Backend Developer"

        question, _ = evidence.cv_gap_question({}, content)

        self.assertEqual(question, "")

    def test_an_undated_role_is_asked_about_by_name(self):
        content = {
            "contact": {"headline": "x", "location": "Pokhara"},
            "sections": [
                {
                    "key": "experience",
                    "entries": [{"title": "Dev", "org": "Phewa Stays", "dates": ""}],
                }
            ],
        }

        question, _ = evidence.cv_gap_question({}, content)

        self.assertIn("When did you work at Phewa Stays", question)

    def test_aiming_at_a_job_does_not_ask_which_role(self):
        content = {"contact": {}, "sections": []}
        state = evidence.set_target({}, title="Backend Developer", missing=[])
        asked = []
        for _ in range(10):
            question, state = evidence.cv_gap_question(state, content)
            if not question:
                break
            asked.append(question)

        self.assertFalse(any("aiming for" in question for question in asked))

    def test_after_a_reply_the_next_gap_is_asked_and_nothing_is_written(self):
        before = dict(self.document.draft.content)

        reply = self.respond()

        self.assertTrue(reply.question)
        self.document.draft.refresh_from_db()
        self.assertEqual(self.document.draft.content, before)

    def test_a_place_already_named_is_not_asked_again(self):
        """Found in the browser: "At Phewa Stays, my current job…" then "where was it?"."""
        question, _ = evidence.cv_gap_question(
            {},
            {"contact": {}, "sections": []},
            said="At Phewa Stays, my current job, I built an internal reporting tool.",
        )

        self.assertEqual(
            question, "What was your title at Phewa Stays, and roughly when did you start there?"
        )


class SkipGapTests(TargetJobFixture):
    def test_skip_at_a_gap_question_moves_on_without_a_model(self):
        """SKIP-01: "skip" to "Have you used Docker?" was sent to the model."""
        assistant_service.set_target(
            document=self.document, title="Backend Developer", missing=["Docker", "Kubernetes"]
        )

        reply = self.respond(text="skip", output=None)

        self.assertIn("Skipped. Nothing about Docker", reply.text)
        self.assertIn("asks for Kubernetes", reply.text)
        self.assertEqual(FakeAdapter.calls, [])
