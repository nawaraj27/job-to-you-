"""
Two-stage fan-out and the delivery ledger (ADR-A25, SRS §13.4).

The four guarantees, each with a test that would fail if the mechanism were
removed:

  * **at-least-once** — a chunk that fails is retried;
  * **exactly-once user-visible effect** — a retried chunk cannot double-send,
    because the ledger's unique constraint stops it, not because a worker
    remembered to check;
  * **nothing sent for a rolled-back transaction** — the event never exists;
  * **chunked** — a large audience becomes many small jobs, not one big one.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import (
    Channel,
    DeliveryStatus,
    FanoutJob,
    FanoutState,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    OutboxEvent,
)
from apps.notifications.services import fanout, outbox


class PlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))

    def _event(self):
        submit(job=self.job, profile=create_profile())
        return OutboxEvent.objects.get(type="application.submitted")

    def test_planning_writes_chunks_for_an_audience(self):
        event = self._event()
        # The apply path's own consumers may already have planned it.
        FanoutJob.objects.filter(event=event).delete()

        self.assertEqual(fanout.plan(event), 1)
        self.assertEqual(FanoutJob.objects.filter(event=event).count(), 1)

    def test_planning_twice_does_not_double_the_work(self):
        event = self._event()
        FanoutJob.objects.filter(event=event).delete()

        fanout.plan(event)
        self.assertEqual(fanout.plan(event), 0)
        self.assertEqual(FanoutJob.objects.filter(event=event).count(), 1)

    def test_an_event_with_no_audience_plans_nothing(self):
        # `job.created` notifies nobody: it is an internal fact.
        from apps.jobs.services import postings

        FanoutJob.objects.all().delete()
        postings.create_job(company=self.company, actor=self.owner, title="Draft role")
        event = OutboxEvent.objects.get(type="job.created")

        self.assertEqual(fanout.plan(event), 0)

    def test_an_empty_audience_plans_nothing(self):
        event = self._event()
        FanoutJob.objects.filter(event=event).delete()
        # Nobody on the team any more.
        self.company.memberships.all().delete()

        self.assertEqual(fanout.plan(event), 0)

    def test_a_large_audience_is_split_into_several_chunks(self):
        # The number ADR-A25 exists for, in miniature: more recipients than one
        # chunk holds becomes several chunks, not one big transaction.
        event = self._event()
        FanoutJob.objects.filter(event=event).delete()

        original = fanout.CHUNK_SIZE
        fanout.CHUNK_SIZE = 1
        try:
            for index in range(3):
                from apps.companies.tests.factories import add_member

                # Admins: the audience is the people who can open the
                # application (D-13), and an unassigned recruiter cannot.
                add_member(self.company, create_user(email=f"m{index}@example.test"), role="admin")
            chunks = fanout.plan(event)
        finally:
            fanout.CHUNK_SIZE = original

        self.assertGreater(chunks, 1)


class DeliveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        submit(job=self.job, profile=self.profile)
        self.event = OutboxEvent.objects.get(type="application.submitted")

    def _run(self):
        FanoutJob.objects.filter(event=self.event).delete()
        fanout.plan(self.event)
        return fanout.run_due()

    def test_running_a_chunk_writes_a_feed_row_for_each_recipient(self):
        self._run()

        self.assertTrue(
            Notification.objects.filter(recipient=self.owner, kind="application.submitted").exists()
        )

    def test_the_notification_carries_the_wording_the_event_declared(self):
        self._run()
        notification = Notification.objects.get(recipient=self.owner)

        self.assertIn(self.job.title, notification.title)

    def test_a_ledger_row_is_written_per_channel(self):
        self._run()

        channels = set(
            NotificationDelivery.objects.filter(event=self.event, recipient=self.owner).values_list(
                "channel", flat=True
            )
        )
        self.assertEqual(channels, {Channel.IN_APP, Channel.EMAIL})

    def test_a_rerun_cannot_double_send(self):
        self._run()
        before = Notification.objects.count()

        # Re-plan and re-run the same event, exactly as a retry would.
        FanoutJob.objects.filter(event=self.event).delete()
        fanout.plan(self.event)
        fanout.run_due()

        self.assertEqual(Notification.objects.count(), before)

    def test_a_muted_group_is_recorded_rather_than_silently_skipped(self):
        NotificationPreference.objects.update_or_create(
            user=self.owner,
            event_key="application_updates",
            defaults={"in_app": False, "email": False},
        )
        self._run()

        statuses = set(
            NotificationDelivery.objects.filter(event=self.event, recipient=self.owner).values_list(
                "status", flat=True
            )
        )
        self.assertEqual(statuses, {DeliveryStatus.MUTED})
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())

    def test_a_chunk_that_finishes_is_marked_done(self):
        self._run()
        self.assertTrue(FanoutJob.objects.filter(event=self.event, state=FanoutState.DONE).exists())

    def test_a_failing_chunk_is_deferred_rather_than_lost(self):
        FanoutJob.objects.filter(event=self.event).delete()
        job = FanoutJob.objects.create(
            event=self.event,
            audience="no_such_audience",
            cursor_start=1,
            cursor_end=10_000,
        )

        result = fanout.run_due()

        job.refresh_from_db()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(job.state, FanoutState.FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertTrue(job.last_error)

    def test_a_chunk_that_keeps_failing_is_parked(self):
        FanoutJob.objects.filter(event=self.event).delete()
        job = FanoutJob.objects.create(
            event=self.event,
            audience="no_such_audience",
            cursor_start=1,
            cursor_end=10_000,
            attempts=fanout.MAX_ATTEMPTS - 1,
        )

        fanout.run_due()

        job.refresh_from_db()
        from django.utils import timezone

        self.assertGreater(job.available_at, timezone.now())


class OutboxIntegrationTests(TestCase):
    """The whole path: a service call ends as a row in somebody's feed."""

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def test_applying_notifies_the_hiring_team_end_to_end(self):
        owner = create_user()
        company, _ = company_with_owner(owner)
        job = publish(create_job(company))

        submit(job=job, profile=create_profile())

        # The outbox drains, which plans; the fan-out worker delivers.
        outbox.drain()
        fanout.run_due()

        self.assertTrue(
            Notification.objects.filter(recipient=owner, kind="application.submitted").exists()
        )

    def test_nothing_is_delivered_for_a_rolled_back_transaction(self):
        from django.db import transaction

        from apps.applications.services import apply as apply_service
        from apps.applications.tests.factories import create_version
        from apps.common.errors import InvalidState

        owner = create_user()
        company, _ = company_with_owner(owner)
        job = publish(create_job(company))
        profile = create_profile()
        version = create_version(profile)

        # Apply, then force the surrounding transaction to roll back.
        try:
            with transaction.atomic():
                apply_service.apply_to_job(
                    job=job, profile=profile, actor=profile.user, resume_version=version
                )
                raise InvalidState("something went wrong afterwards")
        except InvalidState:
            pass

        outbox.drain()
        fanout.run_due()

        # The event never existed, so nothing was ever planned or delivered.
        self.assertFalse(OutboxEvent.objects.filter(type="application.submitted").exists())
        self.assertFalse(Notification.objects.exists())
