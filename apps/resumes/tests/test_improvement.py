"""
From analysis to improvement: the plan, the background jobs, and their honesty.

Covers the two halves of the CV improvement repair:

  * `resumes.improvement` — general findings (no target job needed), each
    mapped to the CV section it is about, classified as a safe rewording or a
    fact only the candidate holds, and re-checked against the current draft;
  * `services.improvements` — answers and rewordings run in the background
    with a persisted, staged state: one active job per CV, idempotent
    resubmission, retry after failure, stall recovery, and nothing written to
    the CV except through an accepted suggestion.

Everything that needs a model runs against `FakeAdapter` through the real
runner, as the analysis and assistant tests do.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.ai.providers.base import ProviderFailure
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.candidates.tests.factories import create_profile
from apps.resumes import content as content_rules
from apps.resumes import improvement
from apps.resumes.analysis import checks as ats
from apps.resumes.analysis import findings as finding_rules
from apps.resumes.models import (
    BuilderMode,
    ImprovementJob,
    ImprovementJobKind,
    ImprovementJobState,
    SuggestionState,
)
from apps.resumes.services import analysis as analysis_service
from apps.resumes.services import builders as builder_service
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import improvements

from .test_analysis import REWRITE_OUTPUT, SUMMARY_OUTPUT, fake_ai
from .test_assistant import SAMPLE_ROLE

GENERIC_SUMMARY = (
    "Hard-working and highly motivated team player, passionate about customers and a "
    "results-driven self-starter who enjoys every challenge at work."
)


def _findings(content):
    today = timezone.localdate()
    return finding_rules.deterministic_findings(
        content, ats_results=ats.ats_checks(content, today=today), today=today
    )


class ImprovementFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        self.profile = create_profile()
        self.actor = self.profile.user
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Weak CV"
        )
        self.draft = self.document.draft
        content = self.draft.content
        content["contact"].update(
            {"name": "Kiran Test", "email": "kiran@example.com", "phone": "", "location": "Pokhara"}
        )
        content_rules.section_by_key(content, "summary")["text"] = GENERIC_SUMMARY
        experience = content_rules.section_by_key(content, "experience")
        self.duty_entry = content_rules.new_entry(
            title="Support Officer",
            org="Fewa Telecom",
            dates="2021 - Present",
            bullets=[
                "Responsible for answering customer calls about billing.",
                "Responsible for updating tickets.",
            ],
        )
        self.empty_entry = content_rules.new_entry(
            title="Sales Assistant", org="Lakeside Mart", dates="2019 - 2021", bullets=[]
        )
        experience["entries"] = [self.duty_entry, self.empty_entry]
        education = content_rules.section_by_key(content, "education")
        education["entries"] = [
            content_rules.new_entry(title="BBS", org="PN Campus", dates="2015 - 2019", bullets=[])
        ]
        self.draft.content = content
        self.draft.save()
        self.document.refresh_from_db()

    def analyse(self):
        from apps.resumes.models import VersionReason
        from apps.resumes.services import versions as version_service

        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        return analysis_service.analyse_version(version=version, actor=self.actor, use_ai=False)

    def codes(self, plan):
        return {item.code for item in plan.items}


# --- general findings -------------------------------------------------------------


class GeneralFindingsTests(ImprovementFixture):
    def test_general_analysis_needs_no_target_job(self):
        analysis = self.analyse()

        self.assertEqual(analysis.target_role, "")
        self.assertIsNone(analysis.job_id)
        codes = set(analysis.findings.values_list("code", flat=True))
        for expected in ("contact_incomplete", "generic_summary", "duty_language", "thin_entry"):
            self.assertIn(expected, codes)

    def test_generic_summary_quotes_the_stock_phrases_actually_there(self):
        found = {f.code: f for f in _findings(self.draft.content)}

        self.assertIn("generic_summary", found)
        self.assertIn("hard-working", found["generic_summary"].text)
        self.assertEqual(found["generic_summary"].section_ref, "summary")

    def test_a_concrete_summary_is_not_called_generic(self):
        content = self.draft.content
        content_rules.section_by_key(content, "summary")["text"] = (
            "Support officer with three years on telecom billing, handling 60 calls a day "
            "and training new starters on Zendesk."
        )

        self.assertNotIn("generic_summary", {f.code for f in _findings(content)})

    def test_one_stock_word_alone_is_not_flagged(self):
        self.assertEqual(
            finding_rules.generic_phrases("A motivated nurse with ten years on wards."),
            ["motivated"],
        )
        content = self.draft.content
        content_rules.section_by_key(content, "summary")["text"] = (
            "A motivated nurse with ten years on surgical wards, now leading a team of six."
        )
        self.assertNotIn("generic_summary", {f.code for f in _findings(content)})

    def test_a_role_with_no_description_is_named(self):
        found = [f for f in _findings(self.draft.content) if f.code == "thin_entry"]

        self.assertEqual(len(found), 1)
        self.assertIn("Sales Assistant", found[0].title)
        self.assertEqual(found[0].section_ref, f"entry.{self.empty_entry['id']}.bullets")

    def test_findings_are_only_raised_for_what_is_absent(self):
        """A complete CV raises none of the structural findings."""
        content = self.draft.content
        content["contact"]["phone"] = "+977 9800000000"
        content_rules.section_by_key(content, "summary")["text"] = (
            "Support officer with three years on telecom billing and ticketing."
        )
        for entry in content_rules.section_by_key(content, "experience")["entries"]:
            entry["bullets"] = ["Answered 60 billing calls a day."]

        codes = {f.code for f in _findings(content)}
        for absent in (
            "thin_entry",
            "untitled_role",
            "thin_project",
            "no_education",
            "contact_incomplete",
        ):
            self.assertNotIn(absent, codes)

    def test_no_education_is_raised_only_when_the_section_is_empty(self):
        content = self.draft.content
        content_rules.section_by_key(content, "education")["entries"] = []

        codes = {f.code for f in _findings(content)}
        self.assertIn("no_education", codes)

    def test_an_empty_role_cannot_be_drafted_only_answered(self):
        analysis = self.analyse()
        finding = analysis.findings.get(code="thin_entry")

        self.assertFalse(finding.is_fixable)


# --- the plan -------------------------------------------------------------------


class PlanTests(ImprovementFixture):
    def test_the_plan_carries_the_analysis_findings(self):
        analysis = self.analyse()
        plan = improvement.plan_for(self.document)

        self.assertEqual(plan.source, "analysis")
        self.assertEqual(plan.analysis, analysis)
        self.assertTrue(
            {"contact_incomplete", "generic_summary", "duty_language", "thin_entry"}
            <= self.codes(plan)
        )

    def test_each_finding_maps_to_the_section_it_is_about(self):
        self.analyse()
        plan = improvement.plan_for(self.document)
        by_code = {item.code: item for item in plan.items}

        self.assertEqual(
            by_code["thin_entry"].affects, "Experience — Sales Assistant at Lakeside Mart"
        )
        self.assertEqual(by_code["thin_entry"].entry_id, self.empty_entry["id"])
        self.assertEqual(by_code["thin_entry"].anchor, f"eb-{self.empty_entry['id']}")
        self.assertEqual(by_code["duty_language"].anchor, f"eb-{self.duty_entry['id']}")
        self.assertEqual(by_code["contact_incomplete"].affects, "Contact details — phone")
        self.assertEqual(by_code["contact_incomplete"].contact_field, "phone")
        self.assertEqual(by_code["generic_summary"].affects, "Summary")

    def test_rewordings_and_missing_facts_are_told_apart(self):
        self.analyse()
        plan = improvement.plan_for(self.document)
        kinds = {item.code: item.kind for item in plan.items}

        self.assertEqual(kinds["generic_summary"], improvement.REWORD)
        self.assertEqual(kinds["duty_language"], improvement.REWORD)
        self.assertEqual(kinds["thin_entry"], improvement.NEEDS_INPUT)
        self.assertEqual(kinds["contact_incomplete"], improvement.NEEDS_INPUT)
        self.assertEqual({i.code for i in plan.draftable}, {"generic_summary", "duty_language"})

    def test_missing_information_becomes_targeted_questions(self):
        self.analyse()
        item = next(i for i in improvement.plan_for(self.document).items if i.code == "thin_entry")

        self.assertTrue(item.can_answer)
        self.assertGreaterEqual(len(item.asks), 2)
        self.assertTrue(item.first_question.endswith("?"))

    def test_a_finding_fixed_since_the_analysis_is_shown_as_done(self):
        self.analyse()
        draft_service.set_contact(draft=self.document.draft, field="phone", value="+977 9800000000")
        self.document.refresh_from_db()

        item = next(
            i for i in improvement.plan_for(self.document).items if i.code == "contact_incomplete"
        )
        self.assertEqual(item.status, improvement.ADDRESSED)

    def test_without_an_analysis_the_plan_is_a_labelled_live_check(self):
        plan = improvement.plan_for(self.document)

        self.assertEqual(plan.source, "live")
        self.assertIn("thin_entry", self.codes(plan))

    def test_strengths_are_not_items(self):
        self.analyse()
        plan = improvement.plan_for(self.document)
        self.assertFalse(any(item.severity == "strength" for item in plan.items))


# --- jobs ---------------------------------------------------------------------------


class JobFixture(ImprovementFixture):
    def submit_turn(self, text="I restocked shelves and ran the till.", token="", **payload):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            job, created = improvements.submit(
                document=self.document,
                actor=self.actor,
                kind=ImprovementJobKind.TURN,
                payload={"text": text, **payload},
                token=token,
            )
        return job, created, callbacks

    def transcript(self):
        return builder_service.session_for(
            document=self.document, mode=BuilderMode.ASSISTANT
        ).transcript


class TurnJobTests(JobFixture):
    def test_the_answer_is_recorded_the_moment_it_is_submitted(self):
        job, created, _ = self.submit_turn()

        self.assertTrue(created)
        self.assertEqual(job.state, ImprovementJobState.QUEUED)
        self.assertEqual(job.stage, "received")
        self.assertEqual(self.transcript()[-1]["role"], "candidate")
        self.assertIn("restocked shelves", self.transcript()[-1]["text"])

    def test_the_work_is_queued_after_commit_not_run_in_the_request(self):
        _job, _created, callbacks = self.submit_turn()
        self.assertEqual(len(callbacks), 1)

    def test_a_second_submission_returns_the_running_job(self):
        first, _, _ = self.submit_turn()
        second, created, callbacks = self.submit_turn(text="Something else.")

        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(len(callbacks), 0)
        self.assertEqual(ImprovementJob.objects.filter(document=self.document).count(), 1)
        # The duplicate did not write a second turn either.
        self.assertEqual(sum(1 for t in self.transcript() if t["role"] == "candidate"), 1)

    def test_the_same_form_token_never_makes_a_second_job(self):
        with self.captureOnCommitCallbacks(execute=True):
            first, _ = improvements.submit(
                document=self.document,
                actor=self.actor,
                kind=ImprovementJobKind.TURN,
                payload={"text": "I restocked shelves."},
                token="abc",
            )
        ImprovementJob.objects.filter(pk=first.pk).update(state=ImprovementJobState.COMPLETED)

        again, created = improvements.submit(
            document=self.document,
            actor=self.actor,
            kind=ImprovementJobKind.TURN,
            payload={"text": "I restocked shelves."},
            token="abc",
        )
        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)

    def test_the_database_allows_one_active_job_per_cv(self):
        from django.db import IntegrityError, transaction

        self.submit_turn()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImprovementJob.objects.create(
                document=self.document,
                kind=ImprovementJobKind.PREPARE,
                state=ImprovementJobState.QUEUED,
            )

    def test_running_drafts_into_the_entry_the_question_was_about(self):
        output = {
            "reply": "Thanks.",
            "role": {"title": "Sales Assistant", "org": "Lakeside Mart", "dates": ""},
            "bullets": [{"text": "Restocked shelves and ran the till.", "why": "Yours."}],
        }
        job, _, _ = self.submit_turn(
            text="I restocked shelves and ran the till.", focus_entry=self.empty_entry["id"]
        )
        with fake_ai():
            FakeAdapter.queue(output)
            outcome = improvements.run(job.pk)

        job.refresh_from_db()
        self.assertEqual(outcome, "completed")
        self.assertEqual(job.state, ImprovementJobState.COMPLETED)
        self.assertEqual(job.result["suggestions"], 1)
        suggestion = self.document.draft.suggestions.get()
        self.assertEqual(suggestion.target_path, f"entry.{self.empty_entry['id']}.bullet")
        # Proposed, never written.
        self.document.draft.refresh_from_db()
        entry = content_rules.entry_by_id(self.document.draft.content, self.empty_entry["id"])[1]
        self.assertEqual(entry["bullets"], [])
        # The answer is in the transcript once, followed by the reply.
        said = [t for t in self.transcript() if t["role"] == "candidate"]
        self.assertEqual(len(said), 1)
        self.assertEqual(self.transcript()[-1]["role"], "assistant")

    def test_an_answer_drafted_into_the_entry_marks_that_point_as_waiting(self):
        self.analyse()
        output = {
            "reply": "Thanks.",
            "role": {"title": "Sales Assistant", "org": "Lakeside Mart", "dates": ""},
            "bullets": [{"text": "Restocked shelves and ran the till.", "why": "Yours."}],
        }
        job, _, _ = self.submit_turn(
            text="I restocked shelves and ran the till.", focus_entry=self.empty_entry["id"]
        )
        with fake_ai():
            FakeAdapter.queue(output)
            improvements.run(job.pk)

        item = next(i for i in improvement.plan_for(self.document).items if i.code == "thin_entry")
        self.assertEqual(item.status, improvement.DRAFTED)
        self.assertEqual(len(item.suggestions), 1)

    def test_nothing_is_drafted_that_was_not_said(self):
        output = {
            "reply": "Thanks.",
            "role": SAMPLE_ROLE,
            "bullets": [
                {"text": "Increased store revenue by 40% across Nepal.", "why": "Invented."}
            ],
        }
        job, _, _ = self.submit_turn(
            text="I restocked shelves.", focus_entry=self.empty_entry["id"]
        )
        with fake_ai():
            FakeAdapter.queue(output)
            improvements.run(job.pk)

        self.assertFalse(self.document.draft.suggestions.exists())

    def test_running_twice_does_the_work_once(self):
        job, _, _ = self.submit_turn()
        with fake_ai():
            FakeAdapter.queue({"reply": "Thanks.", "role": SAMPLE_ROLE, "bullets": []})
            improvements.run(job.pk)
            second = improvements.run(job.pk)

        self.assertTrue(second.startswith("skipped"))

    def test_an_unreachable_model_fails_with_a_plain_message_and_a_retry(self):
        job, _, _ = self.submit_turn()
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            improvements.run(job.pk)

        job.refresh_from_db()
        self.assertEqual(job.state, ImprovementJobState.FAILED)
        self.assertEqual(job.result["error"], improvements.FAILED_AI)
        self.assertNotIn("down", job.result["error"])  # no provider text shown

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            improvements.retry(job=job, actor=self.actor)
        job.refresh_from_db()
        self.assertEqual(job.state, ImprovementJobState.QUEUED)
        self.assertEqual(len(callbacks), 1)

        FakeAdapter.reset()
        with fake_ai():
            FakeAdapter.queue({"reply": "Thanks.", "role": SAMPLE_ROLE, "bullets": []})
            improvements.run(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.state, ImprovementJobState.COMPLETED)
        # A retry answers the same turn; it does not write it again.
        self.assertEqual(sum(1 for t in self.transcript() if t["role"] == "candidate"), 1)

    def test_an_unexpected_error_is_recorded_not_raised(self):
        job, _, _ = self.submit_turn()
        from unittest import mock

        with mock.patch(
            "apps.resumes.services.assistant.respond", side_effect=RuntimeError("secret stack")
        ):
            self.assertEqual(improvements.run(job.pk), "failed")
        job.refresh_from_db()
        self.assertEqual(job.state, ImprovementJobState.FAILED)
        self.assertNotIn("secret", job.result["error"])

    def test_a_stalled_job_is_failed_on_read_and_by_the_sweep(self):
        job, _, _ = self.submit_turn()
        ImprovementJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - improvements.STALL_AFTER - dt.timedelta(minutes=1)
        )

        self.document.refresh_from_db()  # as the next request reads it
        shown = improvements.current_for(self.document)
        self.assertEqual(shown.state, ImprovementJobState.FAILED)
        self.assertEqual(shown.result["error"], improvements.FAILED_STALLED)

    def test_the_sweep_fails_stalled_jobs(self):
        job, _, _ = self.submit_turn()
        ImprovementJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - improvements.STALL_AFTER - dt.timedelta(minutes=1)
        )
        self.assertEqual(improvements.fail_stalled(), 1)

    def test_stages_are_reported_in_order(self):
        job, _, _ = self.submit_turn()
        rows = improvements.stages(job)

        self.assertEqual(
            [r["key"] for r in rows], ["received", "checking", "drafting", "reviewing"]
        )
        self.assertEqual(rows[0]["status"], "current")
        ImprovementJob.objects.filter(pk=job.pk).update(stage="drafting", state="waiting_ai")
        job.refresh_from_db()
        self.assertEqual(
            [r["status"] for r in improvements.stages(job)], ["done", "done", "current", "todo"]
        )

    def test_an_empty_answer_is_refused(self):
        from apps.common.errors import InvalidState

        with self.assertRaises(InvalidState):
            improvements.submit(
                document=self.document,
                actor=self.actor,
                kind=ImprovementJobKind.TURN,
                payload={"text": "  "},
            )
        self.assertFalse(ImprovementJob.objects.exists())


class PrepareJobTests(JobFixture):
    def prepare(self):
        with self.captureOnCommitCallbacks(execute=False):
            job, _ = improvements.submit(
                document=self.document,
                actor=self.actor,
                kind=ImprovementJobKind.PREPARE,
                payload={},
            )
        return job

    def test_prepare_drafts_only_the_safe_rewordings_and_writes_nothing(self):
        self.analyse()
        before = self.document.draft.content
        job = self.prepare()
        with fake_ai():
            # Two bullets to reword, then the summary.
            FakeAdapter.queue(REWRITE_OUTPUT, REWRITE_OUTPUT, SUMMARY_OUTPUT)
            improvements.run(job.pk)

        job.refresh_from_db()
        self.assertEqual(job.state, ImprovementJobState.COMPLETED)
        self.assertEqual(job.result["drafted"], 2)
        paths = set(self.document.draft.suggestions.values_list("target_path", flat=True))
        self.assertEqual(paths, {"summary", f"entry.{self.duty_entry['id']}.bullets"})
        # No suggestion for the empty role: it needs facts, not rewording.
        self.assertNotIn(f"entry.{self.empty_entry['id']}.bullets", paths)
        self.document.draft.refresh_from_db()
        self.assertEqual(self.document.draft.content, before)

    def test_drafts_are_listed_as_proposed_changes_on_the_plan(self):
        self.analyse()
        job = self.prepare()
        with fake_ai():
            FakeAdapter.queue(REWRITE_OUTPUT, REWRITE_OUTPUT, SUMMARY_OUTPUT)
            improvements.run(job.pk)

        plan = improvement.plan_for(self.document)
        self.assertEqual(len(plan.proposed), 2)
        self.assertEqual(
            {i.status for i in plan.items if i.code in ("generic_summary", "duty_language")},
            {improvement.DRAFTED},
        )

    def test_apply_writes_exactly_the_changes_shown(self):
        self.analyse()
        job = self.prepare()
        with fake_ai():
            FakeAdapter.queue(REWRITE_OUTPUT, REWRITE_OUTPUT, SUMMARY_OUTPUT)
            improvements.run(job.pk)
        shown = [s.pk for s in improvement.plan_for(self.document).proposed]

        outcome = improvements.decide_all(
            document=self.document, actor=self.actor, ids=shown[:1], accept=True
        )

        self.assertEqual(outcome, {"done": 1, "refused": 0})
        states = dict(self.document.draft.suggestions.values_list("pk", "state"))
        self.assertEqual(states[shown[0]], SuggestionState.ACCEPTED)
        self.assertEqual(states[shown[1]], SuggestionState.OFFERED)

    def test_reject_leaves_the_cv_exactly_as_it_was(self):
        self.analyse()
        before = self.document.draft.content
        job = self.prepare()
        with fake_ai():
            FakeAdapter.queue(REWRITE_OUTPUT, REWRITE_OUTPUT, SUMMARY_OUTPUT)
            improvements.run(job.pk)
        shown = [s.pk for s in improvement.plan_for(self.document).proposed]

        improvements.decide_all(document=self.document, actor=self.actor, ids=shown, accept=False)

        self.document.draft.refresh_from_db()
        self.assertEqual(self.document.draft.content, before)
        self.assertFalse(
            self.document.draft.suggestions.filter(state=SuggestionState.OFFERED).exists()
        )

    def test_when_nothing_can_be_drafted_the_job_fails_honestly(self):
        self.analyse()
        job = self.prepare()
        with fake_ai():
            FakeAdapter.queue(*([ProviderFailure("down")] * 12))
            improvements.run(job.pk)

        job.refresh_from_db()
        self.assertEqual(job.state, ImprovementJobState.FAILED)
        self.assertFalse(self.document.draft.suggestions.exists())


class AskTests(JobFixture):
    def test_asking_records_the_question_and_remembers_the_entry_once(self):
        improvements.ask(document=self.document, question="What did you do?", focus_entry="e1")

        self.assertEqual(self.transcript()[-1]["text"], "What did you do?")
        self.assertEqual(improvements.take_focus(self.document), "e1")
        self.assertEqual(improvements.take_focus(self.document), "")
