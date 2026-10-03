"""
The evaluation harness end to end, and the background entry points (ADR-A20).

`test_support.EvalHarnessTests` covers `check()` — the assertion vocabulary in
isolation. What is tested here is the part that actually runs in CI: loading the
repository's corpus, driving each case through the real runner, and reporting a
result a build can fail on. A harness whose assertions are correct but which
never reaches a model is a harness that passes forever.

The corpus runs against `FakeAdapter` for the reason stated in the adapter
itself: a golden corpus needs a model that answers the same way twice.
"""

from __future__ import annotations

import datetime as dt
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.ai.models import AIRun, RunStatus
from apps.ai.providers.base import ProviderRefused
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import evals, prompts
from apps.ai.tasks import purge_run_snapshots, run_task
from apps.companies.tests.factories import company_with_owner
from config import ai as ai_config

FAKE_PROVIDER = ai_config.ProviderSpec(
    key="fake", adapter="apps.ai.providers.fake.FakeAdapter", base_url="", timeout_seconds=5
)
FAKE_MODEL = ai_config.ModelSpec(
    provider="fake",
    name="fake-1",
    input_cost_per_million=Decimal("1.00"),
    output_cost_per_million=Decimal("2.00"),
)

#: An output that satisfies `resume_analysis_gaps.json`.
ANALYSIS_OUTPUT = {
    "verdict": "Thin, but salvageable.",
    "headline": "Duties, not outcomes",
    "findings": [
        {"title": "No outcomes", "detail": "Duties, never results.", "severity": "critical"},
        {"title": "Soft skills only", "detail": "No named technology.", "severity": "improve"},
    ],
}
#: An output that satisfies `skills_extract_no_inference.json`.
SKILLS_OUTPUT = {"skills": [{"term": "Django", "evidence": "Built a billing service in Django."}]}


@contextmanager
def every_task_fake():
    """Point the whole task table at the fake provider.

    The corpus names several task keys, so patching one is not enough: a case
    for an unpatched key would reach a real provider chain and fail for a reason
    that has nothing to do with the prompt under test.
    """
    tasks = {
        key: ai_config.TaskSpec(key=key, models=("primary",), cache_hours=spec.cache_hours)
        for key, spec in ai_config.TASKS.items()
    }
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(ai_config.PROVIDERS, {"fake": FAKE_PROVIDER}, clear=True)
        )
        stack.enter_context(mock.patch.dict(ai_config.MODELS, {"primary": FAKE_MODEL}, clear=True))
        stack.enter_context(mock.patch.dict(ai_config.TASKS, tasks, clear=True))
        yield


class HarnessTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()


class CaseLoadingTests(HarnessTestCase):
    def test_the_repository_corpus_loads(self):
        cases = evals.load_cases()
        self.assertTrue(cases)
        self.assertTrue(all(case["name"] for case in cases))

    def test_a_task_filter_narrows_the_corpus(self):
        cases = evals.load_cases(task_key="skills.extract")
        self.assertTrue(cases)
        self.assertTrue(all(case["task_key"] == "skills.extract" for case in cases))

    def test_a_missing_corpus_directory_is_empty_rather_than_an_error(self):
        # A checkout without the corpus should report zero cases, not crash the
        # build step with a FileNotFoundError that reads like a broken harness.
        with mock.patch.object(evals, "EVAL_DIR", "prompts/no-such-directory"):
            self.assertEqual(evals.load_cases(), [])


class CaseRunTests(HarnessTestCase):
    def test_a_case_whose_output_satisfies_its_assertions_passes(self):
        FakeAdapter.queue(ANALYSIS_OUTPUT)
        case = next(c for c in evals.load_cases(task_key="resume.analysis"))

        with every_task_fake():
            result = evals.run_case(case, runner=self._runner())

        self.assertTrue(result.passed, result.failures)
        self.assertEqual(result.task_key, "resume.analysis")
        self.assertNotEqual(result.cost, "0")

    def test_a_case_whose_output_misses_a_field_fails_with_the_reason(self):
        FakeAdapter.queue({"verdict": "Fine.", "headline": "Fine", "findings": []})
        case = next(c for c in evals.load_cases(task_key="resume.analysis"))

        with every_task_fake():
            result = evals.run_case(case, runner=self._runner())

        self.assertFalse(result.passed)
        self.assertTrue(any("findings" in failure for failure in result.failures))

    def test_a_failed_run_fails_the_case_rather_than_raising(self):
        # A provider refusal is a real CI outcome. It must surface as a failed
        # case with the status in it, not as a traceback that hides which case
        # was running.
        FakeAdapter.queue(ProviderRefused("no"))
        case = next(c for c in evals.load_cases(task_key="resume.analysis"))

        with every_task_fake():
            result = evals.run_case(case, runner=self._runner())

        self.assertFalse(result.passed)
        self.assertIn("run failed", result.failures[0])
        self.assertIn(RunStatus.REFUSED, result.failures[0])

    def test_a_case_never_reads_the_cache(self):
        # An evaluation that passes because it read an old answer has evaluated
        # nothing, so the second run must call the provider again.
        FakeAdapter.queue(ANALYSIS_OUTPUT, ANALYSIS_OUTPUT)
        case = next(c for c in evals.load_cases(task_key="resume.analysis"))

        with every_task_fake():
            evals.run_case(case, runner=self._runner())
            evals.run_case(case, runner=self._runner())

        self.assertEqual(len(FakeAdapter.calls), 2)
        self.assertFalse(AIRun.objects.exclude(cached_from=None).exists())

    def test_a_case_can_be_attributed_to_a_user(self):
        FakeAdapter.queue(ANALYSIS_OUTPUT)
        user = create_user()
        case = next(c for c in evals.load_cases(task_key="resume.analysis"))

        with every_task_fake():
            evals.run_case(case, runner=self._runner(), user=user)

        self.assertEqual(AIRun.objects.get().user, user)

    def _runner(self):
        from apps.ai.services import runner

        return runner


class SummaryTests(HarnessTestCase):
    def test_running_the_whole_corpus_summarises_every_case(self):
        FakeAdapter.queue(ANALYSIS_OUTPUT, SKILLS_OUTPUT)

        with every_task_fake():
            summary = evals.run_all()

        self.assertEqual(summary["total"], len(evals.load_cases()))
        self.assertEqual(summary["passed"], summary["total"])
        self.assertEqual(summary["failed"], 0)

    def test_one_bad_case_is_counted_as_failed_without_stopping_the_rest(self):
        # Stopping at the first failure would hide the second, and a prompt
        # change that breaks two cases should report two.
        FakeAdapter.queue({"verdict": "Fine.", "headline": "Fine", "findings": []}, SKILLS_OUTPUT)

        with every_task_fake():
            summary = evals.run_all()

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["passed"], summary["total"] - 1)

    def test_the_filter_restricts_the_run(self):
        FakeAdapter.queue(SKILLS_OUTPUT)

        with every_task_fake():
            summary = evals.run_all(task_key="skills.extract")

        self.assertEqual(summary["total"], 1)
        self.assertTrue(summary["results"][0].passed, summary["results"][0].failures)


class CommandTests(HarnessTestCase):
    def test_a_passing_corpus_exits_cleanly(self):
        FakeAdapter.queue(ANALYSIS_OUTPUT, SKILLS_OUTPUT)
        out = StringIO()

        with every_task_fake():
            call_command("run_evals", stdout=out)

        output = out.getvalue()
        self.assertIn("PASS", output)
        self.assertNotIn("FAIL", output)

    def test_a_failing_case_exits_non_zero(self):
        # The exit code is the whole point: without it CI reports green on a
        # prompt change that degraded quality.
        FakeAdapter.queue({"verdict": "Fine.", "headline": "Fine", "findings": []}, SKILLS_OUTPUT)
        out = StringIO()

        with every_task_fake(), self.assertRaises(SystemExit):
            call_command("run_evals", stdout=out)

        self.assertIn("FAIL", out.getvalue())

    def test_the_failure_reason_is_printed_not_just_the_verdict(self):
        FakeAdapter.queue({"verdict": "Fine.", "headline": "Fine", "findings": []}, SKILLS_OUTPUT)
        out = StringIO()

        with every_task_fake(), self.assertRaises(SystemExit):
            call_command("run_evals", stdout=out)

        self.assertIn("findings", out.getvalue())

    def test_the_task_filter_is_accepted(self):
        FakeAdapter.queue(SKILLS_OUTPUT)
        out = StringIO()

        with every_task_fake():
            call_command("run_evals", "--task", "skills.extract", stdout=out)

        self.assertEqual(out.getvalue().count("PASS"), 1)

    def test_an_empty_corpus_says_so_rather_than_claiming_success(self):
        out = StringIO()
        with mock.patch.object(evals, "EVAL_DIR", "prompts/no-such-directory"):
            call_command("run_evals", stdout=out)
        self.assertIn("no evaluation cases found", out.getvalue())


class BackgroundTaskTests(HarnessTestCase):
    """`apps.ai.tasks` — the same pipeline, off the request path."""

    def test_a_queued_run_returns_the_public_id_of_its_record(self):
        FakeAdapter.queue(ANALYSIS_OUTPUT)
        user = create_user()

        with every_task_fake():
            public_id = run_task(
                task_key="resume.analysis", inputs={"cv_text": "x"}, user_id=user.pk
            )

        run_row = AIRun.objects.get(public_id=public_id)
        self.assertEqual(run_row.status, RunStatus.OK)
        self.assertEqual(run_row.user, user)

    def test_a_queued_run_is_attributed_to_its_company(self):
        FakeAdapter.queue(ANALYSIS_OUTPUT)
        owner = create_user()
        company, _ = company_with_owner(owner)

        with every_task_fake():
            run_task(
                task_key="resume.analysis",
                inputs={"cv_text": "x"},
                user_id=owner.pk,
                company_id=company.pk,
            )

        run_row = AIRun.objects.get()
        self.assertEqual(run_row.company, company)
        self.assertEqual(run_row.user, owner)

    def test_a_deleted_user_does_not_stop_the_run(self):
        # Ids rather than objects means the row may be gone by the time a worker
        # picks the job up. An unattributed run is better than a crashed queue.
        FakeAdapter.queue(ANALYSIS_OUTPUT)

        with every_task_fake():
            run_task(task_key="resume.analysis", inputs={"cv_text": "x"}, user_id=987654)

        self.assertIsNone(AIRun.objects.get().user)

    def test_forcing_a_queued_run_skips_the_cache(self):
        FakeAdapter.queue(ANALYSIS_OUTPUT, ANALYSIS_OUTPUT)

        with every_task_fake():
            run_task(task_key="resume.analysis", inputs={"cv_text": "x"})
            run_task(task_key="resume.analysis", inputs={"cv_text": "x"}, force=True)

        self.assertEqual(len(FakeAdapter.calls), 2)


class SnapshotPurgeTests(HarnessTestCase):
    """Retention on the personal data inside a run (ADR-A11)."""

    def setUp(self):
        super().setUp()
        FakeAdapter.queue(ANALYSIS_OUTPUT)
        with every_task_fake():
            run_task(task_key="resume.analysis", inputs={"cv_text": "Priya Sharma"})
        self.run_row = AIRun.objects.get()

    def test_a_recent_snapshot_is_left_alone(self):
        self.assertEqual(purge_run_snapshots(days=400), 0)
        self.run_row.refresh_from_db()
        self.assertTrue(self.run_row.input_snapshot)

    def test_an_expired_snapshot_is_cleared(self):
        self._age(days=500)

        self.assertEqual(purge_run_snapshots(days=400), 1)

        self.run_row.refresh_from_db()
        self.assertEqual(self.run_row.input_snapshot, {})
        self.assertEqual(self.run_row.raw_output, "")

    def test_the_run_itself_survives_the_purge(self):
        # Cost history and the evaluation corpus outlive the text that produced
        # them. Deleting the row would take both with it.
        self._age(days=500)

        purge_run_snapshots(days=400)

        self.run_row.refresh_from_db()
        self.assertEqual(self.run_row.status, RunStatus.OK)
        self.assertGreater(self.run_row.cost, Decimal("0"))
        self.assertEqual(self.run_row.output, ANALYSIS_OUTPUT)

    def test_purging_twice_clears_nothing_the_second_time(self):
        self._age(days=500)
        purge_run_snapshots(days=400)
        self.assertEqual(purge_run_snapshots(days=400), 0)

    def _age(self, *, days: int) -> None:
        AIRun.objects.filter(pk=self.run_row.pk).update(
            created_at=timezone.now() - dt.timedelta(days=days)
        )
