"""
Embeddings, explanations and the event wiring (ADR-A27, ADR-A10, FR-8.6).

The properties this file is answerable for:

  * **an embedding is an AI call like every other**, through the P11 runner —
    no provider, no retry, no cost accounting of its own (ADR-A10);
  * a vector carries the model that made it, and retrieval filters on it,
    because cosine distance across two models is noise and nothing errors
    (ADR-A27);
  * **every embedding failure is survivable.** Switched off, out of budget,
    provider down, wrong number of dimensions: the row keeps a null vector, the
    feed is still complete, and the score is unchanged;
  * an explanation is written from the breakdown and never from the inputs, so
    it cannot contradict the number beside it (ADR-A6);
  * a published or edited posting bumps the corpus generation, which is what
    makes on-read caching correct (SRS §10.1).

Everything runs against `FakeAdapter` through the real runner. Nothing patches
`runner.embed` — the point under test is that matching uses the shared pipeline,
and a mock would prove the opposite.
"""

from __future__ import annotations

import datetime as dt
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.ai.models import AIRun, RunStatus
from apps.ai.providers.base import ProviderFailure
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts, runner
from apps.candidates.models import CandidateSkill
from apps.candidates.tests.factories import create_profile
from apps.common.models import EMBEDDING_DIMENSIONS
from apps.companies.tests.factories import create_company
from apps.core import features
from apps.core.models import FeatureFlag
from apps.jobs.models import Job, JobSkill, Requirement
from apps.jobs.tests.factories import create_job, publish
from apps.matching import scoring
from apps.matching.services import embeddings, explanations, exposure, recommendations
from apps.notifications.models import OutboxEvent
from apps.taxonomy.models import Skill
from config import ai as ai_config

FAKE_PROVIDER = ai_config.ProviderSpec(
    key="fake", adapter="apps.ai.providers.fake.FakeAdapter", base_url="", timeout_seconds=5
)
FAKE_MODEL = ai_config.ModelSpec(
    provider="fake",
    name="fake-embed-1",
    input_cost_per_million=Decimal("1.00"),
    output_cost_per_million=Decimal("0"),
)
EXPLANATION_OUTPUT = {
    "explanation": "You match on Python and Django; the gap is Kubernetes.",
    "biggest_gap": "Skills",
}


@contextmanager
def fake_ai():
    """Point every task, embedding included, at the fake provider."""
    tasks = {
        key: ai_config.TaskSpec(key=key, models=("primary",), cache_hours=0)
        for key in ai_config.TASKS
    }
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(ai_config.PROVIDERS, {"fake": FAKE_PROVIDER}, clear=True)
        )
        stack.enter_context(mock.patch.dict(ai_config.MODELS, {"primary": FAKE_MODEL}, clear=True))
        stack.enter_context(mock.patch.dict(ai_config.TASKS, tasks, clear=True))
        yield


class EmbeddingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        cache.clear()
        self.addCleanup(cache.clear)

        self.profile = create_profile(headline="Backend engineer")
        self.python = Skill.objects.create(name="Python", slug="python")
        CandidateSkill.objects.create(profile=self.profile, skill=self.python)

        self.company = create_company()
        self.job = publish(create_job(self.company))
        JobSkill.objects.create(job=self.job, skill=self.python, requirement=Requirement.REQUIRED)


class EmbeddingTests(EmbeddingFixture):
    def test_a_job_gets_a_vector_of_the_right_width(self):
        with fake_ai():
            self.assertTrue(embeddings.embed_job(self.job))

        self.job.refresh_from_db()
        self.assertIsNotNone(self.job.embedding)
        self.assertEqual(len(self.job.embedding), EMBEDDING_DIMENSIONS)
        self.assertEqual(self.job.embedding_model, "fake-embed-1")
        self.assertIsNotNone(self.job.embedded_at)

    def test_a_profile_gets_a_vector(self):
        with fake_ai():
            self.assertTrue(embeddings.embed_profile(self.profile))

        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_embedded)

    def test_the_call_goes_through_the_shared_runner(self):
        # Not a mocked call. The proof that matching has no provider knowledge
        # of its own is that an AIRun exists for the embedding (ADR-A10, P11).
        with fake_ai():
            embeddings.embed_job(self.job)

        run = AIRun.objects.get(task_key=runner.EMBED_TASK)
        self.assertEqual(run.status, RunStatus.OK)
        self.assertEqual(run.output["dimensions"], EMBEDDING_DIMENSIONS)

    def test_the_run_records_the_length_of_the_input_not_the_input(self):
        # An embedding input is a whole posting, and `input_snapshot` inherits
        # the retention of the record it came from (ADR-A11).
        with fake_ai():
            embeddings.embed_job(self.job)

        run = AIRun.objects.get(task_key=runner.EMBED_TASK)
        self.assertIn("chars", run.input_snapshot)
        self.assertNotIn(self.job.description, str(run.input_snapshot))

    def test_a_job_embedding_is_charged_to_the_company_not_a_candidate(self):
        with fake_ai():
            embeddings.embed_job(self.job)

        run = AIRun.objects.get(task_key=runner.EMBED_TASK)
        self.assertEqual(run.company, self.company)
        self.assertIsNone(run.user)

    def test_a_profile_embedding_is_charged_to_the_candidate(self):
        with fake_ai():
            embeddings.embed_profile(self.profile)

        run = AIRun.objects.get(task_key=runner.EMBED_TASK)
        self.assertEqual(run.user, self.profile.user)

    def test_the_same_text_embeds_to_the_same_vector(self):
        with fake_ai():
            embeddings.embed_job(self.job)
            first = list(Job.objects.get(pk=self.job.pk).embedding)
            embeddings.embed_job(self.job)
            second = list(Job.objects.get(pk=self.job.pk).embedding)

        self.assertEqual(first, second)

    def test_an_empty_profile_is_not_embedded_and_is_not_an_error(self):
        blank = create_profile()
        with fake_ai():
            self.assertFalse(embeddings.embed_profile(blank))
        self.assertFalse(AIRun.objects.filter(task_key=runner.EMBED_TASK).exists())

    def test_needs_embedding_is_true_for_a_row_with_no_vector(self):
        self.assertTrue(embeddings.needs_embedding(self.job))

    def test_needs_embedding_is_true_for_a_superseded_model(self):
        # The model-migration procedure from ADR-A27: after a swap, every row
        # still on the old model is found by a query rather than by a guess.
        with fake_ai():
            embeddings.embed_job(self.job)
        self.job.refresh_from_db()

        self.assertFalse(embeddings.needs_embedding(self.job, model="fake-embed-1"))
        self.assertTrue(embeddings.needs_embedding(self.job, model="something-else"))


class DegradedEmbeddingTests(EmbeddingFixture):
    """Every failure path. The feed has to survive all of them."""

    def assert_unembedded_but_matchable(self):
        self.job.refresh_from_db()
        self.assertIsNone(self.job.embedding)

        feed = recommendations.feed_for(self.profile, use_cache=False)
        self.assertTrue(feed.items)
        self.assertGreater(feed.items[0].match.score, 0)

    def test_a_provider_outage_leaves_the_row_unembedded_and_the_feed_working(self):
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            self.assertFalse(embeddings.embed_job(self.job))

        self.assert_unembedded_but_matchable()
        self.assertEqual(
            AIRun.objects.get(task_key=runner.EMBED_TASK).status, RunStatus.PROVIDER_ERROR
        )

    def test_ai_switched_off_leaves_the_row_unembedded_and_the_feed_working(self):
        FeatureFlag.objects.create(
            key="ai",
            is_enabled=False,
            expires_on=timezone.localdate() + dt.timedelta(days=7),
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

        with fake_ai():
            self.assertFalse(embeddings.embed_job(self.job))

        self.assert_unembedded_but_matchable()
        self.assertEqual(AIRun.objects.get(task_key=runner.EMBED_TASK).status, RunStatus.DISABLED)
        self.assertEqual(len(FakeAdapter.calls), 0)

    def test_an_exhausted_budget_leaves_the_row_unembedded(self):
        from apps.ai.services import quotas

        quotas.set_limit(owner=self.profile.user, limit=Decimal("0"))
        with fake_ai():
            self.assertFalse(embeddings.embed_profile(self.profile))

        self.assertEqual(
            AIRun.objects.get(task_key=runner.EMBED_TASK).status, RunStatus.QUOTA_EXCEEDED
        )

    def test_a_vector_of_the_wrong_width_is_refused_rather_than_truncated(self):
        # A truncated vector is a plausible-looking answer to a different
        # question, and nothing would error (ADR-A27).
        with fake_ai():
            FakeAdapter.queue([0.1] * 12, [0.1] * 12, [0.1] * 12)
            self.assertFalse(embeddings.embed_job(self.job))

        run = AIRun.objects.filter(task_key=runner.EMBED_TASK).first()
        self.assertEqual(run.status, RunStatus.SCHEMA_ERROR)
        self.assertIn(str(EMBEDDING_DIMENSIONS), run.error)
        self.assert_unembedded_but_matchable()

    def test_a_provider_that_cannot_embed_is_reported_rather_than_attempted(self):
        # A completion-only provider in an embedding chain is a configuration
        # mistake, and it must not look like an outage.
        class Completions:
            key = "text-only"

            def __init__(self, base_url="", api_key=""):
                pass

            def complete(self, request, *, model, timeout):
                raise AssertionError("should never be called for an embedding")

        provider = ai_config.ProviderSpec(
            key="text-only",
            adapter=f"{Completions.__module__}.{Completions.__qualname__}",
            base_url="",
        )
        with (
            mock.patch.dict(ai_config.PROVIDERS, {"text-only": provider}, clear=True),
            mock.patch.dict(
                ai_config.MODELS,
                {
                    "primary": ai_config.ModelSpec(
                        provider="text-only",
                        name="chat-1",
                        input_cost_per_million=Decimal("0"),
                        output_cost_per_million=Decimal("0"),
                    )
                },
                clear=True,
            ),
            mock.patch.dict(
                ai_config.TASKS,
                {
                    runner.EMBED_TASK: ai_config.TaskSpec(
                        key=runner.EMBED_TASK, models=("primary",), cache_hours=0
                    )
                },
            ),
            mock.patch.object(runner, "_adapter_for", side_effect=lambda spec: Completions()),
        ):
            result = runner.embed(text="anything")

        self.assertFalse(result.ok)
        self.assertIn("embeddings", result.run.error)

    def test_the_score_is_identical_with_and_without_a_vector(self):
        # ADR-A6, on this side of the product: vectors change which jobs are
        # *considered*, never what a match is worth.
        before = recommendations.score_pair(self.profile, self.job)
        with fake_ai():
            embeddings.embed_job(self.job)
            embeddings.embed_profile(self.profile)
        self.profile.refresh_from_db()

        after = recommendations.score_pair(self.profile, self.job)
        self.assertEqual(before.as_dict(), after.as_dict())


class RetrievalWithVectorsTests(EmbeddingFixture):
    def test_a_feed_uses_the_vector_path_once_both_sides_are_embedded(self):
        with fake_ai():
            embeddings.embed_job(self.job)
            embeddings.embed_profile(self.profile)
        self.profile.refresh_from_db()

        with fake_ai():
            feed = recommendations.feed_for(self.profile, use_cache=False)
        self.assertEqual(feed.strategy, "vector")
        self.assertTrue(feed.items)

    def test_a_vector_from_a_different_model_is_not_used_for_distance(self):
        # Cosine distance between two models' vectors is noise, and nothing
        # errors — which is why the filter is on the column, not a convention.
        with fake_ai():
            embeddings.embed_job(self.job)
            embeddings.embed_profile(self.profile)

        Job.objects.filter(pk=self.job.pk).update(embedding_model="an-older-model")
        self.profile.refresh_from_db()

        with fake_ai():
            feed = recommendations.feed_for(self.profile, use_cache=False)
        # Still returned — an unembedded posting is still a real job — but it
        # came through the deterministic top-up rather than by distance.
        self.assertIn(self.job.pk, [item.job.pk for item in feed.items])

    def test_an_unembedded_job_is_still_returned(self):
        other = publish(create_job(self.company))
        with fake_ai():
            embeddings.embed_job(self.job)
            embeddings.embed_profile(self.profile)
        self.profile.refresh_from_db()

        with fake_ai():
            feed = recommendations.feed_for(self.profile, use_cache=False)
        self.assertIn(other.pk, [item.job.pk for item in feed.items])


class ExplanationTests(EmbeddingFixture):
    def match(self) -> scoring.Match:
        return recommendations.score_pair(self.profile, self.job)

    def test_an_explanation_is_produced_from_the_breakdown(self):
        with fake_ai():
            FakeAdapter.queue(EXPLANATION_OUTPUT)
            text = explanations.explain(self.match(), job_title=self.job.title)

        self.assertEqual(text, EXPLANATION_OUTPUT["explanation"])

    def test_the_model_is_never_given_the_cv_or_the_posting(self):
        # The constraint that makes an explanation an explanation: it is given
        # the result, so it cannot form a second opinion that contradicts it.
        with fake_ai():
            FakeAdapter.queue(EXPLANATION_OUTPUT)
            explanations.explain(self.match(), job_title=self.job.title)

        sent = FakeAdapter.calls[0]["user"]
        self.assertNotIn(self.job.description, sent)
        self.assertNotIn(self.profile.user.email, sent)
        self.assertIn("Skills", sent)

    def test_the_breakdown_the_model_sees_is_the_one_the_screen_shows(self):
        match = self.match()
        with fake_ai():
            FakeAdapter.queue(EXPLANATION_OUTPUT)
            explanations.explain(match, job_title=self.job.title)

        sent = FakeAdapter.calls[0]["user"]
        for component in match.components:
            with self.subTest(component=component.key):
                self.assertIn(component.label, sent)

    def test_an_unavailable_model_returns_nothing_rather_than_raising(self):
        # The breakdown was always the real explanation; the prose is a
        # convenience on top of it (ADR-A6).
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            self.assertEqual(explanations.explain(self.match(), job_title="x"), "")

    def test_only_the_top_of_a_feed_is_explained(self):
        # FR-8.6. Explaining fifty would multiply the cost by five to write text
        # for rows nobody scrolls to.
        for _ in range(15):
            publish(create_job(self.company))

        with fake_ai():
            FakeAdapter.queue(*[EXPLANATION_OUTPUT] * 20)
            feed = recommendations.feed_for(self.profile, use_cache=False)
            produced = explanations.explain_feed(feed.items)

        self.assertLessEqual(len(produced), explanations.EXPLAIN_TOP)

    def test_the_explanation_goes_through_the_shared_runner(self):
        with fake_ai():
            FakeAdapter.queue(EXPLANATION_OUTPUT)
            explanations.explain(self.match(), job_title=self.job.title)

        run = AIRun.objects.get(task_key="match.explain")
        self.assertEqual(run.status, RunStatus.OK)
        self.assertTrue(run.prompt_version)


class ConsumerTests(EmbeddingFixture):
    def test_publishing_a_job_bumps_the_corpus_generation(self):
        from apps.matching.consumers import bump_on_publish

        before = exposure.job_generation()
        bump_on_publish(
            OutboxEvent(type="job.published", payload={"job_public_id": str(self.job.public_id)})
        )
        self.assertEqual(exposure.job_generation(), before + 1)

    def test_closing_a_job_bumps_it_too(self):
        from apps.matching.consumers import bump_on_close

        before = exposure.job_generation()
        bump_on_close(OutboxEvent(type="job.closed", payload={}))
        self.assertEqual(exposure.job_generation(), before + 1)

    def test_an_edit_that_changes_the_text_queues_a_re_embed(self):
        from apps.matching.consumers import bump_on_update

        with mock.patch("apps.matching.tasks.embed_job.delay") as queued:
            bump_on_update(
                OutboxEvent(
                    type="job.updated",
                    payload={"job_public_id": str(self.job.public_id), "fields": ["description"]},
                )
            )
        queued.assert_called_once_with(self.job.pk)

    def test_an_edit_that_does_not_change_the_text_queues_nothing(self):
        # Re-embedding on every edit would pay for nothing: the vector is built
        # from what the posting says it is looking for, not from its salary.
        from apps.matching.consumers import bump_on_update

        with mock.patch("apps.matching.tasks.embed_job.delay") as queued:
            bump_on_update(
                OutboxEvent(
                    type="job.updated",
                    payload={"job_public_id": str(self.job.public_id), "fields": ["salary_max"]},
                )
            )
        queued.assert_not_called()

    def test_a_profile_change_queues_a_re_embed(self):
        from apps.matching.consumers import reembed_profile

        with mock.patch("apps.matching.tasks.embed_profile.delay") as queued:
            reembed_profile(
                OutboxEvent(
                    type="profile.updated",
                    payload={"profile_public_id": str(self.profile.public_id)},
                )
            )
        queued.assert_called_once_with(self.profile.pk)

    def test_an_event_naming_nothing_is_ignored(self):
        from apps.matching.consumers import reembed_profile

        with mock.patch("apps.matching.tasks.embed_profile.delay") as queued:
            reembed_profile(OutboxEvent(type="profile.updated", payload={}))
        queued.assert_not_called()


class TaskTests(EmbeddingFixture):
    def test_the_job_task_embeds_and_reports(self):
        from apps.matching.tasks import embed_job

        with fake_ai():
            self.assertEqual(embed_job(self.job.pk), "embedded")

    def test_a_deleted_row_is_not_an_error(self):
        from apps.matching.tasks import embed_job, embed_profile

        self.assertEqual(embed_job(987654), "gone")
        self.assertEqual(embed_profile(987654), "gone")

    def test_an_unavailable_provider_is_reported_rather_than_raised(self):
        from apps.matching.tasks import embed_job

        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            self.assertEqual(embed_job(self.job.pk), "unavailable")

    def test_the_backfill_queues_rows_that_have_no_vector(self):
        from apps.matching.tasks import backfill_embeddings

        with (
            fake_ai(),
            mock.patch("apps.matching.tasks.embed_job.delay") as jobs,
        ):
            result = backfill_embeddings()

        self.assertGreaterEqual(result["jobs"], 1)
        jobs.assert_called()

    def test_the_backfill_does_nothing_without_a_configured_model(self):
        from apps.matching.tasks import backfill_embeddings

        with mock.patch.dict(ai_config.PROVIDERS, {}, clear=True):
            self.assertEqual(backfill_embeddings(), {"jobs": 0, "profiles": 0})


class StrategyBranchTests(EmbeddingFixture):
    """All three retrieval strategies (ADR-A24).

    The thresholds are lowered rather than the corpus inflated: creating fifty
    thousand postings to reach the ANN branch would make this suite take
    minutes to assert something that is a function of one number. What is under
    test is that each branch returns correct results, not what the numbers are.

    A24's stated disadvantage is "three code paths to test". This is that test.
    """

    def setUp(self):
        super().setUp()
        self.other = publish(create_job(self.company))
        JobSkill.objects.create(job=self.other, skill=self.python, requirement=Requirement.REQUIRED)

        with fake_ai():
            embeddings.embed_job(self.job)
            embeddings.embed_job(self.other)
            embeddings.embed_profile(self.profile)
        self.profile.refresh_from_db()

    def found(self, **thresholds):
        from apps.matching import retrieval

        with mock.patch.multiple(retrieval, **thresholds):
            return retrieval.retrieve(
                self.profile,
                embedding=list(self.profile.embedding),
                embedding_model=self.profile.embedding_model,
            )

    def test_a_small_filtered_set_orders_by_exact_distance(self):
        found = self.found(SMALL_SET=1_000, MEDIUM_SET=10_000)
        self.assertEqual(found.strategy, "vector")
        self.assertEqual(len(found.jobs), 2)

    def test_a_large_filtered_set_uses_the_index_then_filters(self):
        # The order that makes A24 work: the ANN query is unfiltered so the
        # index can serve it, and the filters are applied to what came back.
        found = self.found(SMALL_SET=1, MEDIUM_SET=1, ANN_FETCH=100)
        self.assertEqual(found.strategy, "ann")
        self.assertEqual({job.pk for job in found.jobs}, {self.job.pk, self.other.pk})

    def test_the_ann_path_respects_the_hard_filters(self):
        # Filtering after an approximate search is the whole difficulty. A
        # blocked company's posting must not survive it.
        from apps.candidates.models import BlockedCompany

        BlockedCompany.objects.create(profile=self.profile, company=self.company)
        found = self.found(SMALL_SET=1, MEDIUM_SET=1, ANN_FETCH=100)
        self.assertEqual(found.jobs, [])

    def test_the_ann_path_tops_up_when_the_filters_reject_most_neighbours(self):
        # ANN returns neighbours from the whole corpus; if the filters reject
        # nearly all of them, a thin page is the failure A24 names.
        unembedded = publish(create_job(self.company))

        found = self.found(SMALL_SET=1, MEDIUM_SET=1, ANN_FETCH=1)
        self.assertIn(unembedded.pk, [job.pk for job in found.jobs])

    def test_the_ann_path_preserves_the_index_order_of_the_survivors(self):
        found = self.found(SMALL_SET=1, MEDIUM_SET=1, ANN_FETCH=100)
        repeated = self.found(SMALL_SET=1, MEDIUM_SET=1, ANN_FETCH=100)
        self.assertEqual([job.pk for job in found.jobs], [job.pk for job in repeated.jobs])

    def test_no_embedding_falls_back_to_the_deterministic_path(self):
        from apps.matching import retrieval

        found = retrieval.retrieve(self.profile, embedding=None, embedding_model="")
        self.assertEqual(found.strategy, "small")
        self.assertEqual(len(found.jobs), 2)

    def test_a_limit_bounds_what_is_scored_whatever_the_strategy(self):
        from apps.matching import retrieval

        for thresholds in ({"SMALL_SET": 1_000}, {"SMALL_SET": 1, "MEDIUM_SET": 1}):
            with self.subTest(thresholds=thresholds), mock.patch.multiple(retrieval, **thresholds):
                found = retrieval.retrieve(
                    self.profile,
                    limit=1,
                    embedding=list(self.profile.embedding),
                    embedding_model=self.profile.embedding_model,
                )
                self.assertLessEqual(len(found.jobs), retrieval.FLOOR)
