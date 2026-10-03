"""
The `paused` posting state (SRS FR-5.1, §7.5), and what "no longer listed" means
on the candidate's own screens.

FR-5.1 is `draft → published → paused → closed → archived`; the model had no
paused state, so an employer who needed a posting off the site for a week could
only close it (a false "filled or cancelled") or put it back to draft (losing
its publication date, and re-firing every alert when it came back).
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.candidates.models import SavedJob
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import JobStatus
from apps.jobs.tests.factories import create_job, create_skill, publish
from apps.notifications.models import OutboxEvent


class PausedFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company, title="Data Analyst"))
        self.act = reverse("employer:job_act", args=[self.company.slug, self.job.public_id])
        self.detail = reverse("employer:job_detail", args=[self.company.slug, self.job.public_id])
        self.client.force_login(self.owner)

    def post(self, action, **extra):
        self.job.refresh_from_db()
        return self.client.post(
            self.act, {"action": action, "row_version": self.job.row_version, **extra}
        )


class PauseResumeTests(PausedFixture):
    def test_the_detail_page_offers_pause_for_a_live_posting(self):
        self.assertContains(self.client.get(self.detail), 'value="pause"')

    def test_pausing_takes_it_off_the_site_and_resuming_puts_it_back(self):
        self.post("pause")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PAUSED)
        self.assertTrue(AuditLog.objects.filter(verb="job.paused").exists())

        candidate = create_profile()
        other = self.client_class()
        other.force_login(candidate.user)
        self.assertEqual(other.get(reverse("jobs:detail", args=[self.job.slug])).status_code, 404)
        self.assertNotContains(other.get(reverse("jobs:list")), "Data Analyst")

        page = self.client.get(self.detail)
        self.assertContains(page, 'value="resume"')
        self.assertNotContains(page, 'value="publish"')

        published_events = OutboxEvent.objects.filter(type="job.published").count()
        self.post("resume")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PUBLISHED)
        # Resuming is not publishing a new role: alerts are not re-fired.
        self.assertEqual(OutboxEvent.objects.filter(type="job.published").count(), published_events)
        self.assertEqual(other.get(reverse("jobs:detail", args=[self.job.slug])).status_code, 200)

    def test_a_paused_posting_can_be_closed(self):
        self.post("pause")
        self.post("close", reason="Filled")

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.CLOSED)

    def test_a_paused_posting_can_still_be_edited(self):
        self.post("pause")
        self.job.refresh_from_db()

        self.assertTrue(self.job.is_editable)
        edit = reverse("employer:job_edit", args=[self.company.slug, self.job.public_id])
        self.assertEqual(self.client.get(edit).status_code, 200)

    def test_resume_refuses_a_posting_that_is_not_paused(self):
        self.post("resume")

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PUBLISHED)
        self.assertFalse(AuditLog.objects.filter(verb="job.resumed").exists())

    def test_pausing_needs_the_publish_grant(self):
        from apps.companies.permissions import Role
        from apps.companies.tests.factories import add_member
        from apps.jobs.models import JobAssignment

        manager = add_member(self.company, create_profile().user, role=Role.HIRING_MANAGER)
        JobAssignment.objects.create(job=self.job, membership=manager)
        self.client.force_login(manager.user)

        self.post("pause")

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PUBLISHED)

    def test_the_list_counts_paused_postings(self):
        self.post("pause")

        page = self.client.get(reverse("employer:job_list", args=[self.company.slug]))

        self.assertContains(page, "1 paused")


class SavedJobLinkTests(PausedFixture):
    def setUp(self):
        super().setUp()
        create_skill("Python")
        self.candidate = create_profile()
        SavedJob.objects.create(profile=self.candidate, job=self.job)
        self.saved_url = reverse("jobs:saved")
        self.job_url = reverse("jobs:detail", args=[self.job.slug])

    def saved_page(self):
        reader = self.client_class()
        reader.force_login(self.candidate.user)
        return reader.get(self.saved_url)

    def test_an_open_saved_job_links_to_its_page(self):
        self.assertContains(self.saved_page(), f'href="{self.job_url}"')

    def test_a_saved_job_that_is_no_longer_listed_does_not_link_to_a_404(self):
        self.post("pause")

        page = self.saved_page()

        self.assertContains(page, "Data Analyst")
        self.assertContains(page, "no longer accepting applications")
        self.assertNotContains(page, f'href="{self.job_url}"')

    def test_a_suspended_companys_posting_is_not_open(self):
        from apps.accounts.tests.factories import create_user
        from apps.companies.services import company as company_service

        company_service.suspend_company(actor=create_user(), company=self.company, reason="x")
        self.job.refresh_from_db()

        self.assertFalse(self.job.is_open)
        self.assertNotContains(self.saved_page(), f'href="{self.job_url}"')
