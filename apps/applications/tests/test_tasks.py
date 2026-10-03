"""
Scheduled work for applications (DEVELOPER.md §3.9).

Thin, and asserted for what a thin task can still get wrong: being wired to the
wrong service, and not being on the beat schedule at all. A task nobody
schedules does not exist in production.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.applications.models import Offer, OfferState
from apps.applications.services import offers as offer_service
from apps.applications.tasks import expire_due_offers, sweep_stale_applications
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish


class TaskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())

    def test_the_task_expires_an_overdue_offer(self):
        offer = offer_service.draft_offer(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            salary="900000",
            currency="NPR",
        )
        offer_service.send(offer=offer, actor=self.owner, membership=self.membership)
        Offer.objects.filter(pk=offer.pk).update(expires_at=timezone.now() - dt.timedelta(hours=1))

        self.assertEqual(expire_due_offers(), 1)
        offer.refresh_from_db()
        self.assertEqual(offer.state, OfferState.EXPIRED)

    def test_the_task_is_safe_with_nothing_to_do(self):
        self.assertEqual(expire_due_offers(), 0)

    def test_the_stale_sweep_counts_and_closes_nothing(self):
        from apps.applications.models import Application

        Application.objects.filter(pk=self.application.pk).update(
            last_activity_at=timezone.now() - dt.timedelta(days=90)
        )

        self.assertEqual(sweep_stale_applications(days=30), 1)

        self.application.refresh_from_db()
        # Auto-closing would be the platform rejecting somebody on the
        # employer's behalf (FR-6.7).
        self.assertTrue(self.application.is_live)


class ScheduleTests(TestCase):
    def test_both_application_tasks_are_scheduled(self):
        from config.celery import app

        tasks = {entry["task"] for entry in app.conf.beat_schedule.values()}
        self.assertIn("apps.applications.tasks.expire_due_offers", tasks)
        self.assertIn("apps.applications.tasks.sweep_stale_applications", tasks)
