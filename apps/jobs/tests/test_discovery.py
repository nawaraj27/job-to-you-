"""
Saving, saved searches, alerts and view counting (ADR-A21).

The view counter gets its own attention because the number an employer uses to
judge a posting is worthless if a refresh inflates it, or if the company's own
team is counted.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.candidates.models import AlertFrequency, SavedJob, SavedSearch
from apps.candidates.tests.factories import create_profile
from apps.common.errors import Conflict, InvalidState
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs import selectors
from apps.jobs.models import Job, JobAlertMatch, JobView
from apps.jobs.search import SearchQueryInput
from apps.jobs.services import discovery, lifecycle
from apps.jobs.tests.factories import create_job, publish


class SaveTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.company, _ = company_with_owner(create_user(email="owner@example.test"))
        self.job = publish(create_job(self.company))

    def test_saving_is_idempotent(self):
        discovery.save_job(profile=self.profile, actor=self.user, job=self.job)
        discovery.save_job(profile=self.profile, actor=self.user, job=self.job)
        self.assertEqual(SavedJob.objects.count(), 1)

    def test_an_unpublished_job_cannot_be_saved(self):
        draft = create_job(self.company)
        with self.assertRaises(InvalidState):
            discovery.save_job(profile=self.profile, actor=self.user, job=draft)

    def test_unsaving_removes_the_row(self):
        discovery.save_job(profile=self.profile, actor=self.user, job=self.job)
        discovery.unsave_job(profile=self.profile, actor=self.user, job=self.job)
        self.assertEqual(SavedJob.objects.count(), 0)

    def test_unsaving_something_that_was_never_saved_is_quiet(self):
        discovery.unsave_job(profile=self.profile, actor=self.user, job=self.job)
        self.assertEqual(SavedJob.objects.count(), 0)

    def test_a_saved_job_that_later_closes_stays_on_the_shortlist(self):
        discovery.save_job(profile=self.profile, actor=self.user, job=self.job)
        owner_membership = self.company.memberships.first()
        lifecycle.close(job=self.job, actor=owner_membership.user, membership=owner_membership)

        # Dropping it silently would lose the record of what someone was
        # working through; the screen shows it as closed instead.
        self.assertEqual(selectors.saved_jobs_for(self.profile).count(), 1)

    def test_saving_is_audited(self):
        discovery.save_job(profile=self.profile, actor=self.user, job=self.job)
        self.assertTrue(AuditLog.objects.filter(verb="job.saved").exists())


class SavedSearchTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.profile = create_profile(user=self.user)

    def save(self, name="Python roles", **kwargs):
        return discovery.save_search(
            profile=self.profile,
            actor=self.user,
            name=name,
            query=kwargs.pop("query", SearchQueryInput(text="python")),
            **kwargs,
        )

    def test_a_search_stores_its_filters_rather_than_a_url(self):
        record = self.save()
        self.assertEqual(record.query, {"q": "python"})

    def test_an_empty_search_is_refused(self):
        with self.assertRaises(InvalidState):
            self.save(query=SearchQueryInput())

    def test_a_search_needs_a_name(self):
        with self.assertRaises(InvalidState):
            self.save(name="  ")

    def test_two_searches_cannot_share_a_name(self):
        self.save()
        with self.assertRaises(Conflict):
            self.save(name="python roles")

    def test_the_number_of_saved_searches_is_capped(self):
        for index in range(discovery.MAX_SAVED_SEARCHES):
            self.save(name=f"Search {index}")
        with self.assertRaises(InvalidState):
            self.save(name="One too many")

    def test_an_unknown_alert_frequency_is_refused(self):
        with self.assertRaises(InvalidState):
            self.save(alert_frequency="hourly")

    def test_changing_the_frequency_is_audited(self):
        record = self.save()
        discovery.set_alert(
            saved_search=record, actor=self.user, alert_frequency=AlertFrequency.DAILY
        )
        record.refresh_from_db()
        self.assertEqual(record.alert_frequency, AlertFrequency.DAILY)
        self.assertTrue(AuditLog.objects.filter(verb="search.alert_changed").exists())

    def test_a_saved_search_is_scoped_to_its_owner(self):
        record = self.save()
        stranger = create_profile(user=create_user(email="stranger@example.test"))
        self.assertIsNone(selectors.saved_search_for(stranger, record.pk))
        self.assertIsNotNone(selectors.saved_search_for(self.profile, record.pk))


class ViewCountTests(TestCase):
    def setUp(self):
        self.owner = create_user(email="owner@example.test")
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company, title="Python Engineer"))

    def test_a_view_counts_once(self):
        self.assertTrue(discovery.record_view(job=self.job, viewer_key="session:abc"))
        self.job.refresh_from_db()
        self.assertEqual(self.job.views_count, 1)

    def test_a_refresh_inside_the_window_does_not_count_again(self):
        discovery.record_view(job=self.job, viewer_key="session:abc")
        self.assertFalse(discovery.record_view(job=self.job, viewer_key="session:abc"))

        self.job.refresh_from_db()
        self.assertEqual(self.job.views_count, 1)

    def test_the_same_person_counts_again_after_the_window(self):
        discovery.record_view(job=self.job, viewer_key="session:abc")
        JobView.objects.update(
            created_at=timezone.now() - dt.timedelta(hours=discovery.VIEW_WINDOW_HOURS + 1)
        )
        self.assertTrue(discovery.record_view(job=self.job, viewer_key="session:abc"))

    def test_two_people_count_separately(self):
        discovery.record_view(job=self.job, viewer_key="session:abc")
        discovery.record_view(job=self.job, viewer_key="session:xyz")
        self.job.refresh_from_db()
        self.assertEqual(self.job.views_count, 2)

    def test_the_hiring_team_does_not_inflate_its_own_number(self):
        self.assertFalse(discovery.record_view(job=self.job, viewer_key="user:1", user=self.owner))
        self.job.refresh_from_db()
        self.assertEqual(self.job.views_count, 0)

    def test_a_colleague_is_also_excluded(self):
        colleague = create_user(email="colleague@example.test")
        add_member(self.company, colleague, Role.RECRUITER)
        self.assertFalse(discovery.record_view(job=self.job, viewer_key="user:2", user=colleague))

    def test_an_unpublished_posting_counts_nothing(self):
        draft = create_job(self.company)
        self.assertFalse(discovery.record_view(job=draft, viewer_key="session:abc"))

    def test_the_viewer_hash_does_not_store_who_it_was(self):
        discovery.record_view(job=self.job, viewer_key="session:abc")
        stored = JobView.objects.values_list("viewer_hash", flat=True).first()
        self.assertNotIn("abc", stored)


class AlertTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.company, _ = company_with_owner(create_user(email="owner@example.test"))
        self.search = discovery.save_search(
            profile=self.profile,
            actor=self.user,
            name="Python roles",
            query=SearchQueryInput(text="python"),
            alert_frequency=AlertFrequency.DAILY,
        )

    def test_a_matching_job_produces_a_match_row(self):
        publish(create_job(self.company, title="Python Engineer"))
        result = discovery.run_alerts(frequency=AlertFrequency.DAILY)

        self.assertEqual(result["matches"], 1)
        self.assertEqual(JobAlertMatch.objects.count(), 1)

    def test_a_second_run_tells_nobody_twice(self):
        publish(create_job(self.company, title="Python Engineer"))
        discovery.run_alerts(frequency=AlertFrequency.DAILY)
        second = discovery.run_alerts(frequency=AlertFrequency.DAILY)

        self.assertEqual(second["matches"], 0)
        self.assertEqual(JobAlertMatch.objects.count(), 1)

    def test_only_jobs_published_since_the_last_run_are_reported(self):
        discovery.run_alerts(frequency=AlertFrequency.DAILY)
        old = publish(create_job(self.company, title="Python Engineer"))
        Job.objects.filter(pk=old.pk).update(published_at=timezone.now() - dt.timedelta(days=3))

        self.assertEqual(discovery.run_alerts(frequency=AlertFrequency.DAILY)["matches"], 0)

    def test_a_search_on_another_schedule_is_left_alone(self):
        publish(create_job(self.company, title="Python Engineer"))
        result = discovery.run_alerts(frequency=AlertFrequency.WEEKLY)
        self.assertEqual(result["searches"], 0)

    def test_a_run_emits_an_event_so_the_notification_has_a_source(self):
        publish(create_job(self.company, title="Python Engineer"))
        discovery.run_alerts(frequency=AlertFrequency.DAILY)

        from apps.notifications.models import OutboxEvent

        self.assertTrue(OutboxEvent.objects.filter(type="job.alert_matched").exists())

    def test_a_run_stamps_the_search_so_the_next_one_knows_where_to_start(self):
        discovery.run_alerts(frequency=AlertFrequency.DAILY)
        self.search.refresh_from_db()
        self.assertIsNotNone(self.search.last_run_at)

    def test_deleting_a_search_takes_its_matches_with_it(self):
        publish(create_job(self.company, title="Python Engineer"))
        discovery.run_alerts(frequency=AlertFrequency.DAILY)
        discovery.delete_search(saved_search=self.search, actor=self.user)

        self.assertEqual(SavedSearch.objects.count(), 0)
        self.assertEqual(JobAlertMatch.objects.count(), 0)
