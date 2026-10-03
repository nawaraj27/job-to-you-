"""
The run pipeline (SRS §5.11, ADR-A11, ADR-A28).

P11's exit criterion, clause by clause: **a task runs, caches, is attributed,
and survives a provider outage.** Each is a test here, and each would fail if
the corresponding mechanism were removed.

Everything runs against `FakeAdapter` — a real adapter implementing the real
protocol, registered like any other provider. The thing under test is the
pipeline, and every part of it is provider-agnostic by construction; testing it
against a patched `requests` would be testing the patch.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.ai.models import AIRun, PromptTemplate, RunStatus
from apps.ai.providers.base import ProviderFailure, ProviderRefused, ProviderTimeout
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts, runner
from apps.companies.tests.factories import company_with_owner
from apps.notifications.models import OutboxEvent
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


@contextmanager
def fake_routing(*, models=("primary",), **task_overrides):
    """Point one task at the fake provider, with the chain the test needs.

    A context manager rather than three separate patches at each call site:
    routing is one decision, and a test that patched two of the three would
    fail in a way that looks like a runner bug.
    """
    model_specs = dict.fromkeys(models, FAKE_MODEL)
    task = ai_config.TaskSpec(key="resume.analysis", models=tuple(models), **task_overrides)

    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(ai_config.PROVIDERS, {"fake": FAKE_PROVIDER}, clear=True)
        )
        stack.enter_context(mock.patch.dict(ai_config.MODELS, model_specs, clear=True))
        stack.enter_context(mock.patch.dict(ai_config.TASKS, {"resume.analysis": task}))
        yield


class RunnerTestCase(TestCase):
    """Shared setup: a loaded prompt and a clean fake."""

    @classmethod
    def setUpTestData(cls):
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        self.user = create_user()

    def run_task(self, **kwargs):
        kwargs.setdefault("task_key", "resume.analysis")
        kwargs.setdefault("inputs", {"cv_text": "Priya Sharma, developer."})
        kwargs.setdefault("user", self.user)
        return runner.run(**kwargs)

    def valid_output(self):
        """An output satisfying the current `resume.analysis` schema.

        The schema is the contract, and it moved in P12: `headline` is required
        and severity uses the report's vocabulary. A fixture that drifts from
        the prompt tests nothing about the prompt.
        """
        return {
            "verdict": "Thin, but salvageable.",
            "headline": "Duties, not outcomes",
            "findings": [{"title": "No outcomes", "detail": "Add numbers.", "severity": "improve"}],
        }


class SuccessTests(RunnerTestCase):
    def test_a_task_runs_and_records_everything(self):
        FakeAdapter.queue(self.valid_output())

        with fake_routing():
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.OK)
        self.assertEqual(run_row.output["verdict"], "Thin, but salvageable.")
        self.assertEqual(run_row.provider, "fake")
        self.assertEqual(run_row.model, "fake-1")
        self.assertGreater(run_row.tokens_in, 0)
        self.assertGreater(run_row.cost, Decimal("0"))
        self.assertTrue(run_row.raw_output)

    def test_the_prompt_version_is_recorded(self):
        FakeAdapter.queue(self.valid_output())
        template = PromptTemplate.objects.get(task_key="resume.analysis")

        with fake_routing():
            run_row = self.run_task()

        self.assertEqual(run_row.prompt_version, template.version)

    def test_the_prompt_is_filled_from_the_inputs(self):
        FakeAdapter.queue(self.valid_output())

        with fake_routing():
            self.run_task(inputs={"cv_text": "UNIQUE-MARKER-TEXT"})

        self.assertIn("UNIQUE-MARKER-TEXT", FakeAdapter.calls[0]["user"])

    def test_a_run_emits_an_event_without_the_output_in_it(self):
        FakeAdapter.queue(self.valid_output())

        with fake_routing():
            self.run_task()

        event = OutboxEvent.objects.get(type="ai.run_completed")
        self.assertEqual(event.payload["status"], RunStatus.OK)
        self.assertNotIn("output", event.payload)


class CacheTests(RunnerTestCase):
    def test_the_second_identical_request_does_not_call_the_provider(self):
        FakeAdapter.queue(self.valid_output())

        with fake_routing(cache_hours=24):
            first = self.run_task()
            second = self.run_task()

        self.assertEqual(len(FakeAdapter.calls), 1)
        self.assertEqual(second.cached_from_id, first.pk)
        self.assertEqual(second.output, first.output)

    def test_a_cache_hit_costs_nothing_but_is_still_a_row(self):
        FakeAdapter.queue(self.valid_output())

        with fake_routing(cache_hours=24):
            self.run_task()
            second = self.run_task()

        # The second request is a fact, it belongs to whoever made it, and
        # `cached_from` is what makes "does this cache help" answerable.
        self.assertEqual(second.cost, Decimal("0"))
        self.assertEqual(second.tokens_in, 0)
        self.assertEqual(AIRun.objects.count(), 2)

    def test_different_inputs_do_not_share_a_cache_entry(self):
        FakeAdapter.queue(self.valid_output(), self.valid_output())

        with fake_routing(cache_hours=24):
            self.run_task(inputs={"cv_text": "one"})
            self.run_task(inputs={"cv_text": "two"})

        self.assertEqual(len(FakeAdapter.calls), 2)

    def test_key_order_does_not_change_the_cache_key(self):
        # Two structurally identical inputs must hash the same however the
        # caller built the dict, or the cache misses constantly.
        first = runner.input_hash("t", 1, {"a": 1, "b": 2})
        second = runner.input_hash("t", 1, {"b": 2, "a": 1})
        self.assertEqual(first, second)

    def test_a_new_prompt_version_invalidates_the_cache(self):
        # The behaviour a prompt change must have, or an improvement never
        # reaches anybody.
        FakeAdapter.queue(self.valid_output(), self.valid_output())

        with fake_routing(cache_hours=24):
            self.run_task()

            template = PromptTemplate.objects.get(task_key="resume.analysis")
            PromptTemplate.objects.create(
                task_key="resume.analysis",
                version=template.version + 1,
                body=template.body + "\n\nAlso mention formatting.",
                body_sha256="b" * 64,
                output_schema=template.output_schema,
                source_path=template.source_path,
            )

            self.run_task()

        self.assertEqual(len(FakeAdapter.calls), 2)

    def test_force_skips_the_cache(self):
        FakeAdapter.queue(self.valid_output(), self.valid_output())

        with fake_routing(cache_hours=24):
            self.run_task()
            self.run_task(force=True)

        self.assertEqual(len(FakeAdapter.calls), 2)

    def test_a_task_with_no_cache_window_never_reuses(self):
        # Two people asking for a cover letter for the same job should not get
        # the same letter.
        FakeAdapter.queue(self.valid_output(), self.valid_output())

        with fake_routing(cache_hours=0):
            self.run_task()
            self.run_task()

        self.assertEqual(len(FakeAdapter.calls), 2)


class FailureTests(RunnerTestCase):
    """Retry classification: what is worth another provider and what is not."""

    def test_a_timeout_falls_through_to_the_next_provider(self):
        FakeAdapter.queue(ProviderTimeout("too slow"), self.valid_output())

        with fake_routing(models=("primary", "backup")):
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.OK)
        self.assertEqual(run_row.attempt, 2)
        self.assertEqual(AIRun.objects.count(), 2)
        self.assertEqual(AIRun.objects.filter(status=RunStatus.TIMEOUT).count(), 1)

    def test_a_provider_failure_falls_through_too(self):
        FakeAdapter.queue(ProviderFailure("HTTP 503"), self.valid_output())

        with fake_routing(models=("primary", "backup")):
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.OK)

    def test_a_refusal_stops_rather_than_asking_again(self):
        # A refusal is a decision about the content. Asking elsewhere spends
        # money to reach the same answer.
        FakeAdapter.queue(ProviderRefused("declined"), self.valid_output())

        with fake_routing(models=("primary", "backup")):
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.REFUSED)
        self.assertEqual(len(FakeAdapter.calls), 1)

    def test_a_schema_error_stops_rather_than_asking_again(self):
        # The same prompt will produce the same shape from the next provider.
        FakeAdapter.queue({"wrong": "shape"}, self.valid_output())

        with fake_routing(models=("primary", "backup")):
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.SCHEMA_ERROR)
        # The message names the field, so a prompt review is actionable.
        self.assertIn("verdict", run_row.error)
        self.assertEqual(len(FakeAdapter.calls), 1)

    def test_the_raw_output_survives_a_schema_error(self):
        # The only way to debug one.
        FakeAdapter.queue("not json at all")

        with fake_routing():
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.SCHEMA_ERROR)
        self.assertEqual(run_row.raw_output, "not json at all")

    def test_every_provider_failing_still_returns_a_run(self):
        FakeAdapter.queue(ProviderTimeout("a"), ProviderTimeout("b"))

        with fake_routing(models=("primary", "backup")):
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.TIMEOUT)
        self.assertFalse(run_row.succeeded)

    def test_an_unexpected_adapter_error_is_a_provider_error_not_a_crash(self):
        FakeAdapter.queue(ValueError("adapter bug"))

        with fake_routing():
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.PROVIDER_ERROR)

    def test_a_failed_call_still_costs_money_and_is_charged(self):
        # A budget that only counts successes is not a budget. This one fails
        # on schema, which means the provider answered and billed.
        FakeAdapter.queue({"wrong": "shape"})

        with fake_routing():
            run_row = self.run_task()

        self.assertGreater(run_row.cost, Decimal("0"))

    def test_no_configured_provider_is_recorded_rather_than_raised(self):
        empty = mock.patch.dict(ai_config.PROVIDERS, {}, clear=True)
        with empty:
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.PROVIDER_ERROR)
        self.assertIn("No configured provider", run_row.error)


class KillSwitchTests(RunnerTestCase):
    def switch_off(self) -> None:
        import datetime as dt

        from django.utils import timezone

        from apps.core import features
        from apps.core.models import FeatureFlag

        FeatureFlag.objects.create(
            key="ai",
            is_enabled=False,
            expires_on=timezone.localdate() + dt.timedelta(days=7),
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

    def test_the_switch_stops_dispatch_and_says_so(self):
        self.switch_off()

        with fake_routing():
            run_row = self.run_task()

        self.assertEqual(run_row.status, RunStatus.DISABLED)
        self.assertIn("switched off", run_row.error)
        self.assertEqual(len(FakeAdapter.calls), 0)

    def test_the_switch_is_not_recorded_as_a_provider_failure(self):
        # A deliberate shutdown and a provider outage look identical on an
        # error-rate dashboard and are opposites. Folding one into the other
        # fires the alert during the incident you already know about, and buries
        # the real provider failures under it.
        self.switch_off()

        with fake_routing():
            run_row = self.run_task()

        self.assertNotIn(run_row.status, RunStatus.retryable())
        self.assertIn(run_row.status, RunStatus.terminal())

    def test_the_switch_is_runtime_rather_than_a_deploy(self):
        # ADR-A28's one deliberate exception: everything else about provider
        # configuration is code, but stopping an incident cannot wait.
        self.assertTrue(runner.is_enabled())


class AttributionTests(RunnerTestCase):
    def test_a_run_is_attributed_to_its_user(self):
        FakeAdapter.queue(self.valid_output())

        with fake_routing():
            run_row = self.run_task()

        self.assertEqual(run_row.user, self.user)

    def test_a_run_is_attributed_to_a_company_when_one_is_given(self):
        FakeAdapter.queue(self.valid_output())
        company, _ = company_with_owner(create_user(email="owner@example.test"))

        with fake_routing():
            run_row = self.run_task(company=company)

        self.assertEqual(run_row.company, company)

    def test_the_input_reference_points_back_at_what_caused_it(self):
        FakeAdapter.queue(self.valid_output())

        with fake_routing():
            run_row = self.run_task(input_ref=self.user)

        self.assertEqual(run_row.input_ref, self.user)
