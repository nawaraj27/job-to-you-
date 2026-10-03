"""
Scheduled work (DEVELOPER.md §3.9).

Thin by design — the tasks delegate to services that are tested directly. What
is asserted here is that they are wired to the right service, run eagerly in a
test, and are registered on the beat schedule. A task nobody schedules is a
promise the product makes on screen and never keeps.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.candidates.models import AlertFrequency
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import Job, JobAlertMatch, JobStatus
from apps.jobs.search import SearchQueryInput
from apps.jobs.services import discovery
from apps.jobs.tasks import expire_due_jobs, run_job_alerts
from apps.jobs.tests.factories import create_job, publish


class ExpiryTaskTests(TestCase):
    def setUp(self):
        self.company, _ = company_with_owner(create_user())

    def test_the_task_expires_an_overdue_posting(self):
        job = publish(create_job(self.company))
        Job.objects.filter(pk=job.pk).update(closes_at=timezone.now() - dt.timedelta(hours=1))

        self.assertEqual(expire_due_jobs(), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.EXPIRED)

    def test_the_task_is_safe_to_run_when_there_is_nothing_to_do(self):
        self.assertEqual(expire_due_jobs(), 0)


class AlertTaskTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.company, _ = company_with_owner(create_user(email="owner@example.test"))
        discovery.save_search(
            profile=self.profile,
            actor=self.user,
            name="Python roles",
            query=SearchQueryInput(text="python"),
            alert_frequency=AlertFrequency.DAILY,
        )

    def test_the_daily_run_reports_new_matches(self):
        publish(create_job(self.company, title="Python Engineer"))

        result = run_job_alerts(frequency=AlertFrequency.DAILY)

        self.assertEqual(result["matches"], 1)
        self.assertEqual(JobAlertMatch.objects.count(), 1)

    def test_the_weekly_run_leaves_daily_searches_alone(self):
        publish(create_job(self.company, title="Python Engineer"))
        self.assertEqual(run_job_alerts(frequency=AlertFrequency.WEEKLY)["searches"], 0)


class ScheduleTests(TestCase):
    """A task that is not on the beat schedule does not exist in production."""

    def test_both_job_tasks_are_scheduled(self):
        from config.celery import app

        tasks = {entry["task"] for entry in app.conf.beat_schedule.values()}
        self.assertIn("apps.jobs.tasks.expire_due_jobs", tasks)
        self.assertIn("apps.jobs.tasks.run_job_alerts", tasks)

    def test_alerts_are_scheduled_for_each_frequency_a_candidate_can_choose(self):
        from config.celery import app

        scheduled = {
            entry.get("kwargs", {}).get("frequency")
            for entry in app.conf.beat_schedule.values()
            if entry["task"] == "apps.jobs.tasks.run_job_alerts"
        }
        chosen = set(AlertFrequency.values) - {AlertFrequency.OFF}
        self.assertEqual(scheduled, chosen)
