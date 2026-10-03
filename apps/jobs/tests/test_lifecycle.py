"""
The posting lifecycle (SRS §7.4, §7.5).

Most of what follows is about a transition being *refused*. A state machine
whose illegal moves are untested is indistinguishable from a `status` field
somebody sets by hand — the legal moves pass either way.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.common.errors import InvalidState, NotAllowed, StaleWrite
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs.models import Job, JobStatus
from apps.jobs.services import lifecycle
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import OutboxEvent


class PublishTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)

    def publish(self, job, **kwargs):
        return lifecycle.publish(job=job, actor=self.user, membership=self.membership, **kwargs)

    def test_publishing_makes_the_job_visible_and_dates_it(self):
        job = create_job(self.company)
        self.publish(job)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.PUBLISHED)
        self.assertIsNotNone(job.published_at)
        self.assertIn(job, Job.objects.published())

    def test_publishing_without_a_description_is_refused(self):
        job = create_job(self.company, description="")
        with self.assertRaises(InvalidState) as caught:
            self.publish(job)
        self.assertIn("a description", caught.exception.message)

    def test_publishing_without_a_salary_is_refused_while_pay_is_shown(self):
        job = create_job(self.company, salary_min=None, salary_max=None)
        with self.assertRaises(InvalidState):
            self.publish(job)

    def test_hiding_pay_is_an_answer_rather_than_a_gap(self):
        job = create_job(self.company, salary_min=None, salary_max=None, salary_visible=False)
        self.publish(job)
        self.assertEqual(job.status, JobStatus.PUBLISHED)

    def test_publishing_with_a_closing_date_in_the_past_is_refused(self):
        job = create_job(self.company, closes_at=timezone.now() - dt.timedelta(days=1))
        with self.assertRaises(InvalidState):
            self.publish(job)

    def test_an_external_posting_needs_its_url(self):
        job = create_job(self.company, apply_mode="external", external_url="https://x.test/a")
        job.external_url = ""
        with self.assertRaises(InvalidState):
            self.publish(job)

    def test_a_member_without_the_publish_grant_is_refused(self):
        other = create_user(email="interviewer@example.test")
        membership = add_member(self.company, other, Role.INTERVIEWER)
        job = create_job(self.company)

        with self.assertRaises(NotAllowed):
            lifecycle.publish(job=job, actor=other, membership=membership)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.DRAFT)

    def test_publishing_emits_an_event_and_writes_an_audit_row(self):
        job = create_job(self.company)
        self.publish(job)

        self.assertTrue(OutboxEvent.objects.filter(type="job.published").exists())
        self.assertTrue(AuditLog.objects.filter(verb="job.published").exists())

    def test_a_stale_row_version_is_refused(self):
        job = create_job(self.company)
        with self.assertRaises(StaleWrite):
            self.publish(job, row_version=job.row_version + 5)

    def test_publishing_bumps_the_row_version(self):
        job = create_job(self.company)
        before = job.row_version
        self.publish(job, row_version=before)
        self.assertEqual(job.row_version, before + 1)


class TransitionTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)
        self.job = publish(create_job(self.company))

    def test_a_draft_cannot_be_closed_without_being_published(self):
        draft = create_job(self.company)
        with self.assertRaises(InvalidState):
            lifecycle.close(job=draft, actor=self.user, membership=self.membership)

    def test_an_archived_job_cannot_go_straight_back_to_published(self):
        lifecycle.close(job=self.job, actor=self.user, membership=self.membership)
        lifecycle.archive(job=self.job, actor=self.user, membership=self.membership)

        with self.assertRaises(InvalidState):
            lifecycle.publish(job=self.job, actor=self.user, membership=self.membership)

    def test_closing_records_why(self):
        lifecycle.close(job=self.job, actor=self.user, membership=self.membership, reason="Filled")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.CLOSED)
        self.assertEqual(self.job.closed_reason, "Filled")
        self.assertIsNotNone(self.job.closed_at)

    def test_a_closed_job_is_gone_from_public_reads(self):
        lifecycle.close(job=self.job, actor=self.user, membership=self.membership)
        self.assertNotIn(self.job, Job.objects.published())

    def test_reopening_a_closed_job_clears_the_closure(self):
        lifecycle.close(job=self.job, actor=self.user, membership=self.membership, reason="Filled")
        lifecycle.publish(job=self.job, actor=self.user, membership=self.membership)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PUBLISHED)
        self.assertEqual(self.job.closed_reason, "")
        self.assertIsNone(self.job.closed_at)

    def test_restore_brings_an_archived_job_back_as_a_draft(self):
        lifecycle.unpublish(job=self.job, actor=self.user, membership=self.membership)
        lifecycle.archive(job=self.job, actor=self.user, membership=self.membership)
        lifecycle.restore(job=self.job, actor=self.user, membership=self.membership)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.DRAFT)
        self.assertIsNone(self.job.archived_at)

    def test_available_lists_only_what_this_actor_could_do(self):
        interviewer = add_member(
            self.company, create_user(email="i@example.test"), Role.INTERVIEWER
        )
        targets = lifecycle.LIFECYCLE.available(self.job, interviewer.grants)
        # Closing needs no grant; returning to draft does.
        self.assertIn(JobStatus.CLOSED, targets)
        self.assertNotIn(JobStatus.DRAFT, targets)


class ExpiryTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)

    def _overdue(self, job):
        Job.objects.filter(pk=job.pk).update(closes_at=timezone.now() - dt.timedelta(hours=1))

    def test_a_posting_past_its_closing_date_expires(self):
        job = publish(create_job(self.company))
        self._overdue(job)

        self.assertEqual(lifecycle.expire_due(), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.EXPIRED)
        self.assertIsNotNone(job.closed_at)

    def test_expiry_is_distinct_from_a_closure_somebody_decided_on(self):
        job = publish(create_job(self.company))
        lifecycle.close(job=job, actor=self.user, membership=self.membership)
        self._overdue(job)

        # Already out of published: their decision stands, and the sweep does
        # not quietly relabel it.
        self.assertEqual(lifecycle.expire_due(), 0)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.CLOSED)

    def test_a_posting_with_no_closing_date_never_expires(self):
        publish(create_job(self.company, closes_at=None))
        self.assertEqual(lifecycle.expire_due(), 0)

    def test_expiry_emits_an_event_so_watchers_are_told(self):
        job = publish(create_job(self.company))
        self._overdue(job)
        lifecycle.expire_due()
        self.assertTrue(OutboxEvent.objects.filter(type="job.expired").exists())

    def test_expiry_is_idempotent(self):
        job = publish(create_job(self.company))
        self._overdue(job)
        lifecycle.expire_due()
        self.assertEqual(lifecycle.expire_due(), 0)


class DatabaseConstraintTests(TestCase):
    """The guarantees that survive a bug in the service layer."""

    def setUp(self):
        self.company, _ = company_with_owner(create_user())

    def test_an_inverted_salary_range_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_job(self.company, salary_min=Decimal("200000"), salary_max=Decimal("100000"))

    def test_a_remote_posting_without_a_scope_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_job(self.company, workplace="remote", remote_scope="none")

    def test_a_published_posting_without_a_date_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_job(self.company, status=JobStatus.PUBLISHED, published_at=None)

    def test_an_external_posting_without_a_url_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_job(self.company, apply_mode="external", external_url="")
