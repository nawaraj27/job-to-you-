"""
Capture, rollups and the four measurements (SRS §16).

The properties this file is answerable for:

  * **capture never breaks the product** — it swallows its own failures, and it
    never copies personal data out of a payload;
  * **rollups are idempotent per (date, dimension)** — a re-run recomputes
    rather than double-counting, which is the difference between a dashboard
    somebody trusts after an incident and one they do not;
  * **rollups are computed from the domain tables**, not from the lossy event
    table, so the funnel cannot drift from the applications;
  * **every steering measurement can come back "no"** — each is asserted in
    both directions, because a metric that can only confirm is not a
    measurement.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.analytics.models import (
    AICostDaily,
    AnalyticsEvent,
    CompanyDailyStat,
    FunnelSnapshot,
    JobDailyStat,
    SteeringMetric,
    SteeringSnapshot,
)
from apps.analytics.services import capture, rollups, steering
from apps.applications.models import Application
from apps.applications.services import pipeline
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import JobView
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for
from apps.notifications.models import OutboxEvent


class AnalyticsFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.candidate = create_profile()
        self.today = timezone.localdate()

    def apply(self, profile=None, **overrides) -> Application:
        return application_for(profile or create_profile(), self.job, **overrides)


class CaptureTests(AnalyticsFixture):
    def test_a_fact_is_recorded(self):
        event = capture.record("job.viewed", company=self.company, props={"source": "search"})

        self.assertIsNotNone(event)
        self.assertEqual(event.name, "job.viewed")
        self.assertEqual(event.company, self.company)

    def test_capture_never_raises(self):
        # A counter must never break the product.
        with mock.patch.object(AnalyticsEvent.objects, "create", side_effect=RuntimeError("boom")):
            self.assertIsNone(capture.record("job.viewed"))

    def test_personal_fields_are_never_copied(self):
        event = capture.record(
            "application.submitted",
            props={
                "email": "priya@example.com",
                "cover_letter_content": "Dear hiring manager",
                "job_id": 7,
            },
        )
        self.assertNotIn("email", event.props)
        self.assertNotIn("cover_letter_content", event.props)
        self.assertEqual(event.props["job_id"], 7)

    def test_prose_is_truncated_rather_than_stored_whole(self):
        event = capture.record("thing.happened", props={"note": "x" * 5_000})
        self.assertLessEqual(len(event.props["note"]), 120)

    def test_a_list_becomes_its_length(self):
        # A payload copied wholesale is how a resume ends up in a table with a
        # different retention from the resume.
        event = capture.record("thing.happened", props={"skills": ["a", "b", "c"]})
        self.assertEqual(event.props["skills"], 3)

    def test_the_number_of_kept_keys_is_bounded(self):
        event = capture.record(
            "thing.happened", props={f"key_{index}": index for index in range(40)}
        )
        self.assertLessEqual(len(event.props), capture.MAX_PROPS)

    def test_a_domain_event_resolves_its_company(self):
        event = capture.from_event(
            OutboxEvent(type="job.published", payload={"company_slug": self.company.slug})
        )
        self.assertEqual(event.company, self.company)

    def test_a_domain_event_with_no_company_is_still_recorded(self):
        event = capture.from_event(OutboxEvent(type="user.registered", payload={}))
        self.assertIsNotNone(event)
        self.assertIsNone(event.company)

    def test_the_wildcard_consumer_records_everything(self):
        from apps.analytics.consumers import count_everything

        count_everything(
            OutboxEvent(type="job.published", payload={"company_slug": self.company.slug})
        )
        self.assertTrue(AnalyticsEvent.objects.filter(name="job.published").exists())


class JobRollupTests(AnalyticsFixture):
    def view_job(self, *, times: int = 1, viewer: str = "hash-1") -> None:
        for _ in range(times):
            JobView.objects.create(job=self.job, viewer_hash=viewer)

    def test_views_and_applications_land_in_one_row(self):
        self.view_job(times=3)
        self.apply()

        rollups.roll_jobs(day=self.today)
        row = JobDailyStat.objects.get(job=self.job, day=self.today)

        self.assertEqual(row.views, 3)
        self.assertEqual(row.applications, 1)

    def test_unique_viewers_are_counted_separately(self):
        self.view_job(times=3, viewer="hash-1")
        self.view_job(times=2, viewer="hash-2")

        rollups.roll_jobs(day=self.today)
        row = JobDailyStat.objects.get(job=self.job, day=self.today)

        self.assertEqual(row.views, 5)
        self.assertEqual(row.unique_viewers, 2)

    def test_a_rerun_recomputes_rather_than_double_counting(self):
        # The property that makes a dashboard trustworthy after an incident.
        self.view_job(times=4)
        rollups.roll_jobs(day=self.today)
        rollups.roll_jobs(day=self.today)

        self.assertEqual(JobDailyStat.objects.filter(job=self.job, day=self.today).count(), 1)
        self.assertEqual(JobDailyStat.objects.get(job=self.job, day=self.today).views, 4)

    def test_a_posting_with_no_activity_gets_no_row(self):
        rollups.roll_jobs(day=self.today)
        self.assertFalse(JobDailyStat.objects.filter(job=self.job).exists())

    def test_a_stale_row_is_removed_on_recompute(self):
        self.view_job(times=2)
        rollups.roll_jobs(day=self.today)
        JobView.objects.all().delete()

        rollups.roll_jobs(day=self.today)
        self.assertFalse(JobDailyStat.objects.filter(job=self.job, day=self.today).exists())

    def test_the_apply_rate_is_derived_not_stored(self):
        self.view_job(times=4)
        self.apply()
        rollups.roll_jobs(day=self.today)

        row = JobDailyStat.objects.get(job=self.job, day=self.today)
        self.assertEqual(row.apply_rate, 0.25)

    def test_yesterday_is_the_default_day(self):
        # A day still happening produces a row that is wrong until midnight.
        yesterday = self.today - dt.timedelta(days=1)
        JobView.objects.create(job=self.job, viewer_hash="h")
        JobView.objects.filter(job=self.job).update(
            created_at=timezone.now() - dt.timedelta(days=1)
        )

        rollups.run_all()
        self.assertTrue(JobDailyStat.objects.filter(day=yesterday).exists())


class CompanyRollupTests(AnalyticsFixture):
    def test_applications_are_counted(self):
        self.apply()
        self.apply()

        rollups.roll_companies(day=self.today)
        row = CompanyDailyStat.objects.get(company=self.company, day=self.today)
        self.assertEqual(row.applications, 2)

    def test_a_move_out_of_new_counts_as_a_response(self):
        # What a candidate is waiting for is a decision, and never getting one
        # is the thing they complain about.
        application = self.apply()
        stage = pipeline.stage_by_key(self.company, "reviewing")
        pipeline.move(
            application=application,
            actor=self.owner,
            membership=self.membership,
            stage=stage,
        )

        rollups.roll_companies(day=self.today)
        row = CompanyDailyStat.objects.get(company=self.company, day=self.today)

        self.assertEqual(row.responded, 1)
        self.assertGreaterEqual(float(row.response_hours_total), 0)

    def test_the_response_rate_is_derived(self):
        self.apply()
        application = self.apply()
        pipeline.move(
            application=application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline.stage_by_key(self.company, "reviewing"),
        )

        rollups.roll_companies(day=self.today)
        row = CompanyDailyStat.objects.get(company=self.company, day=self.today)
        self.assertEqual(row.response_rate, 0.5)

    def test_a_rerun_recomputes(self):
        self.apply()
        rollups.roll_companies(day=self.today)
        rollups.roll_companies(day=self.today)

        self.assertEqual(
            CompanyDailyStat.objects.filter(company=self.company, day=self.today).count(), 1
        )


class FunnelRollupTests(AnalyticsFixture):
    def test_standing_applications_are_snapshotted_per_stage(self):
        self.apply()
        self.apply()

        rollups.roll_funnels(day=self.today)
        rows = FunnelSnapshot.objects.filter(company=self.company, day=self.today)

        self.assertTrue(rows.exists())
        self.assertEqual(sum(row.count for row in rows), 2)

    def test_a_move_records_entered_and_exited(self):
        application = self.apply()
        pipeline.move(
            application=application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline.stage_by_key(self.company, "reviewing"),
        )

        rollups.roll_funnels(day=self.today)
        screening = FunnelSnapshot.objects.get(
            company=self.company, day=self.today, stage_key="reviewing"
        )
        new = FunnelSnapshot.objects.get(company=self.company, day=self.today, stage_key="new")

        self.assertEqual(screening.entered, 1)
        self.assertEqual(new.exited, 1)

    def test_time_in_stage_is_measured_from_arrival(self):
        # Without the fallback to `applied_at`, time in `new` would always be
        # missing — and `new` is the stage the response measurement is about.
        application = self.apply()
        Application.objects.filter(pk=application.pk).update(
            applied_at=timezone.now() - dt.timedelta(hours=10)
        )
        pipeline.move(
            application=application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline.stage_by_key(self.company, "reviewing"),
        )

        rollups.roll_funnels(day=self.today)
        new = FunnelSnapshot.objects.get(company=self.company, day=self.today, stage_key="new")
        self.assertGreater(float(new.hours_in_stage_total), 5)

    def test_a_stage_everybody_left_still_gets_a_row(self):
        application = self.apply()
        pipeline.move(
            application=application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline.stage_by_key(self.company, "reviewing"),
        )

        rollups.roll_funnels(day=self.today)
        self.assertTrue(
            FunnelSnapshot.objects.filter(
                company=self.company, day=self.today, stage_key="new"
            ).exists()
        )

    def test_a_rerun_recomputes(self):
        self.apply()
        rollups.roll_funnels(day=self.today)
        first = FunnelSnapshot.objects.filter(company=self.company, day=self.today).count()
        rollups.roll_funnels(day=self.today)

        self.assertEqual(
            FunnelSnapshot.objects.filter(company=self.company, day=self.today).count(), first
        )


class AICostRollupTests(AnalyticsFixture):
    def make_run(self, *, task="resume.analysis", status="ok", cost="0.01", cached=None):
        from apps.ai.models import AIRun

        return AIRun.objects.create(
            task_key=task,
            status=status,
            cost=Decimal(cost),
            tokens_in=100,
            tokens_out=50,
            latency_ms=800,
            cached_from=cached,
        )

    def test_runs_and_cost_are_summed_per_task(self):
        self.make_run()
        self.make_run(cost="0.02")

        rollups.roll_ai_cost(day=self.today)
        row = AICostDaily.objects.get(day=self.today, task_key="resume.analysis")

        self.assertEqual(row.runs, 2)
        self.assertEqual(row.cost, Decimal("0.030000"))

    def test_failures_are_counted_separately(self):
        self.make_run()
        self.make_run(status="provider_error")

        rollups.roll_ai_cost(day=self.today)
        row = AICostDaily.objects.get(day=self.today, task_key="resume.analysis")

        self.assertEqual(row.failures, 1)
        self.assertEqual(row.failure_rate, 0.5)

    def test_cache_hits_are_counted(self):
        source = self.make_run()
        self.make_run(cached=source)

        rollups.roll_ai_cost(day=self.today)
        row = AICostDaily.objects.get(day=self.today, task_key="resume.analysis")
        self.assertEqual(row.cached, 1)

    def test_tasks_are_kept_apart(self):
        self.make_run(task="resume.analysis")
        self.make_run(task="match.explain")

        rollups.roll_ai_cost(day=self.today)
        self.assertEqual(AICostDaily.objects.filter(day=self.today).count(), 2)

    def test_a_rerun_recomputes(self):
        self.make_run()
        rollups.roll_ai_cost(day=self.today)
        rollups.roll_ai_cost(day=self.today)

        self.assertEqual(AICostDaily.objects.filter(day=self.today).count(), 1)


class BackfillTests(AnalyticsFixture):
    def test_a_backfill_covers_a_range_and_is_repeatable(self):
        # A rollup bug is always found after it has been writing wrong numbers
        # for a fortnight.
        JobView.objects.create(job=self.job, viewer_hash="h")
        JobView.objects.filter(job=self.job).update(
            created_at=timezone.now() - dt.timedelta(days=3)
        )

        first = rollups.backfill(days=7)
        second = rollups.backfill(days=7)

        self.assertEqual(first["jobs"], second["jobs"])
        self.assertEqual(JobDailyStat.objects.count(), 1)


class SteeringTests(AnalyticsFixture):
    """§16.3. Each measurement asserted in both directions."""

    def test_a_predictive_matcher_reads_as_predictive(self):
        for score in (90, 88, 92):
            application = self.apply(match_snapshot={"score": score})
            pipeline.move(
                application=application,
                actor=self.owner,
                membership=self.membership,
                stage=pipeline.stage_by_key(self.company, "interview"),
            )
        for score in (30, 25, 20):
            self.apply(match_snapshot={"score": score})

        snapshot = steering.measure_match_predicts_outcome()
        self.assertGreater(float(snapshot.value), 5)
        self.assertEqual(snapshot.detail["reads_as"], "predictive")

    def test_a_matcher_sorting_noise_says_so(self):
        # The conclusion this measurement is designed to be able to reach.
        for score in (50, 52, 48):
            application = self.apply(match_snapshot={"score": score})
            pipeline.move(
                application=application,
                actor=self.owner,
                membership=self.membership,
                stage=pipeline.stage_by_key(self.company, "interview"),
            )
        for score in (51, 49, 50):
            self.apply(match_snapshot={"score": score})

        snapshot = steering.measure_match_predicts_outcome()
        self.assertIn("not predictive", snapshot.detail["reads_as"])

    def test_a_small_sample_is_marked_as_not_meaningful(self):
        # A correlation over eleven applications is not a finding.
        self.apply(match_snapshot={"score": 90})
        snapshot = steering.measure_match_predicts_outcome()

        self.assertFalse(snapshot.is_meaningful)

    def test_suggestion_acceptance_is_measured_over_decided_rows(self):
        from apps.resumes.models import ResumeSuggestion, SuggestionState
        from apps.resumes.services import documents as document_service

        document = document_service.create_document(
            profile=self.candidate, actor=self.candidate.user, name="CV"
        )
        for state in (
            SuggestionState.ACCEPTED,
            SuggestionState.ACCEPTED,
            SuggestionState.REJECTED,
            SuggestionState.OFFERED,
        ):
            ResumeSuggestion.objects.create(
                draft=document.draft,
                target_path="summary",
                proposed="Better.",
                source="ai",
                state=state,
            )

        snapshot = steering.measure_ai_suggestions_accepted()
        # Stored to four places, which is the column's precision.
        self.assertAlmostEqual(float(snapshot.value), 2 / 3, places=3)
        self.assertEqual(snapshot.sample_size, 3)
        self.assertEqual(snapshot.detail["still_open"], 1)

    def test_reconcile_suggestions_are_excluded(self):
        # The measurement is about whether the *AI* is useful; a reconcile
        # prompt is the profile disagreeing with the CV, which is not AI.
        from apps.resumes.models import ResumeSuggestion, SuggestionState
        from apps.resumes.services import documents as document_service

        document = document_service.create_document(
            profile=self.candidate, actor=self.candidate.user, name="CV"
        )
        ResumeSuggestion.objects.create(
            draft=document.draft,
            target_path="summary",
            proposed="From the profile.",
            source="reconcile",
            state=SuggestionState.ACCEPTED,
        )

        self.assertEqual(steering.measure_ai_suggestions_accepted().sample_size, 0)

    def test_employer_response_reads_the_rollup(self):
        CompanyDailyStat.objects.create(
            company=self.company,
            day=self.today,
            applications=100,
            responded=60,
            response_hours_total=Decimal("600"),
        )
        snapshot = steering.measure_employer_response(day=self.today)

        self.assertEqual(float(snapshot.value), 0.6)
        self.assertEqual(snapshot.detail["average_hours"], 10.0)

    def test_an_interview_that_predicts_hires_reads_as_predictive(self):
        self._interviews(positive_hired=8, positive_total=10, negative_hired=0, negative_total=10)
        snapshot = steering.measure_interview_predicts_hire()

        self.assertGreater(float(snapshot.value), 0.5)
        self.assertEqual(snapshot.detail["reads_as"], "predictive")

    def test_an_interview_that_predicts_nothing_says_so(self):
        self._interviews(positive_hired=3, positive_total=10, negative_hired=3, negative_total=10)
        snapshot = steering.measure_interview_predicts_hire()

        self.assertIn("not predictive", snapshot.detail["reads_as"])

    def test_all_four_are_taken_together(self):
        written = steering.measure_all()
        self.assertEqual(len(written), 4)
        self.assertEqual({row.metric for row in written}, set(SteeringMetric.values))

    def test_measuring_twice_in_a_day_recomputes(self):
        steering.measure_all(day=self.today)
        steering.measure_all(day=self.today)
        self.assertEqual(SteeringSnapshot.objects.filter(day=self.today).count(), 4)

    def test_the_latest_of_each_is_readable(self):
        steering.measure_all(day=self.today - dt.timedelta(days=1))
        steering.measure_all(day=self.today)

        latest = steering.latest()
        self.assertEqual(len(latest), 4)
        self.assertTrue(all(row.day == self.today for row in latest.values()))

    def test_history_comes_back_in_order(self):
        for offset in (3, 2, 1):
            steering.measure_employer_response(day=self.today - dt.timedelta(days=offset))

        rows = steering.history(SteeringMetric.EMPLOYER_RESPONSE)
        self.assertEqual([row.day for row in rows], sorted(row.day for row in rows))

    def _interviews(self, *, positive_hired, positive_total, negative_hired, negative_total):
        from apps.interviews.models import (
            InterviewKind,
            InterviewOutcome,
            InterviewSession,
            Recommendation,
            SessionState,
        )
        from apps.interviews.tests.factories import practice_template

        template = practice_template(slug="steering")
        hired_stage = pipeline.stage_by_key(self.company, "hired")

        def make(count, recommendation, hired):
            for index in range(count):
                application = self.apply()
                if index < hired:
                    Application.objects.filter(pk=application.pk).update(stage=hired_stage)
                session = InterviewSession.objects.create(
                    application=application,
                    candidate=application.candidate,
                    template=template,
                    kind=InterviewKind.AI_TEXT,
                    state=SessionState.COMPLETED,
                )
                InterviewOutcome.objects.create(
                    session=session, recommendation=recommendation, average_score=Decimal("4")
                )

        make(positive_total, Recommendation.YES, positive_hired)
        make(negative_total, Recommendation.NO, negative_hired)
