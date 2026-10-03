"""
The nightly analytics work (SRS §16.1, §5.15).

The tasks are thin wrappers, so what is worth asserting is the two places a
wrapper can still be wrong:

  * **the day argument round-trips as a string**, because that is how a date
    survives a Celery serialisation boundary, and a task that quietly rolled up
    *today* every time it was handed a date would look like it worked;
  * **pruning is bounded and only touches expired rows** — an unbounded delete
    over a partition-sized table takes a lock nobody planned for, and a prune
    that took one row too many would be deleting history the rollups have
    already been computed from.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.analytics.models import (
    AnalyticsEvent,
    CompanyDailyStat,
    JobDailyStat,
    SteeringSnapshot,
)
from apps.analytics.tasks import (
    EVENT_RETENTION_DAYS,
    backfill_rollups,
    measure_steering,
    prune_events,
    run_rollups,
)
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import JobView
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for


class TaskFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.today = timezone.localdate()
        self.yesterday = self.today - dt.timedelta(days=1)

    def activity_on(self, day: dt.date) -> None:
        """A view and an application, dated into the given day."""
        JobView.objects.create(job=self.job, viewer_hash="hash-1")
        JobView.objects.filter(job=self.job).update(
            created_at=timezone.make_aware(dt.datetime.combine(day, dt.time(12, 0)))
        )
        application = application_for(create_profile(), self.job)
        type(application).objects.filter(pk=application.pk).update(
            applied_at=timezone.make_aware(dt.datetime.combine(day, dt.time(12, 0)))
        )


class RollupTaskTests(TaskFixture):
    def test_a_named_day_is_the_day_rolled_up(self):
        # The date crosses the queue as a string; if it were dropped the task
        # would roll up today and look perfectly healthy doing it.
        self.activity_on(self.today - dt.timedelta(days=3))
        run_rollups(day=(self.today - dt.timedelta(days=3)).isoformat())

        self.assertTrue(JobDailyStat.objects.filter(day=self.today - dt.timedelta(days=3)).exists())
        self.assertFalse(JobDailyStat.objects.filter(day=self.today).exists())

    def test_no_argument_rolls_up_yesterday(self):
        self.activity_on(self.yesterday)
        counts = run_rollups()

        self.assertTrue(JobDailyStat.objects.filter(day=self.yesterday).exists())
        self.assertEqual(set(counts), {"jobs", "companies", "funnels", "ai"})

    def test_a_rerun_does_not_double_count(self):
        self.activity_on(self.yesterday)
        run_rollups()
        run_rollups()

        self.assertEqual(CompanyDailyStat.objects.filter(day=self.yesterday).count(), 1)

    def test_backfill_covers_a_range(self):
        self.activity_on(self.today - dt.timedelta(days=5))
        backfill_rollups(days=10)

        self.assertTrue(JobDailyStat.objects.filter(day=self.today - dt.timedelta(days=5)).exists())


class SteeringTaskTests(TaskFixture):
    def test_the_measurements_are_taken(self):
        taken = measure_steering()

        self.assertEqual(taken, 4)
        self.assertEqual(SteeringSnapshot.objects.count(), 4)

    def test_a_named_day_is_the_day_measured(self):
        measure_steering(day=self.yesterday.isoformat())
        self.assertEqual(SteeringSnapshot.objects.filter(day=self.yesterday).count(), 4)


class PruneTests(TaskFixture):
    def stale_event(self, *, age_days: int) -> AnalyticsEvent:
        event = AnalyticsEvent.objects.create(name="job.viewed")
        AnalyticsEvent.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - dt.timedelta(days=age_days)
        )
        return event

    def test_expired_events_are_removed(self):
        self.stale_event(age_days=EVENT_RETENTION_DAYS + 1)
        self.assertEqual(prune_events(), 1)
        self.assertFalse(AnalyticsEvent.objects.exists())

    def test_events_inside_retention_are_kept(self):
        # The rollups are computed from the domain tables, but a prune that took
        # one row too many would still be deleting the only record of the facts
        # nobody built a table for.
        self.stale_event(age_days=10)
        self.assertEqual(prune_events(), 0)
        self.assertEqual(AnalyticsEvent.objects.count(), 1)

    def test_the_batch_is_bounded(self):
        for _ in range(3):
            self.stale_event(age_days=EVENT_RETENTION_DAYS + 1)

        self.assertEqual(prune_events(limit=2), 2)
        self.assertEqual(AnalyticsEvent.objects.count(), 1)

    def test_nothing_to_prune_is_not_an_error(self):
        self.assertEqual(prune_events(), 0)
