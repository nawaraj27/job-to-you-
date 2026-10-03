"""
Candidate job screens, proved through real HTTP.

Two properties matter most here and neither is provable from the service tests:

  * **browsing needs no account.** A job board that asks for a sign-up before
    it shows a salary has lost the reader, so the anonymous path is asserted
    rather than assumed;
  * **an unpublished posting is a 404**, never a 403. A URL that behaves
    differently for a draft tells a stranger that the company has one.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.candidates.models import AlertFrequency, SavedJob, SavedSearch
from apps.candidates.tests.factories import create_profile
from apps.companies.models import CompanyState
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import JobView
from apps.jobs.tests.factories import create_job, publish


class BrowseTests(TestCase):
    def setUp(self):
        self.company, _ = company_with_owner(create_user(email="owner@example.test"))
        self.job = publish(create_job(self.company, title="Senior Python Engineer"))

    def test_the_list_is_open_to_anyone(self):
        response = self.client.get(reverse("jobs:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Senior Python Engineer")

    def test_a_draft_never_appears_in_the_list(self):
        create_job(self.company, title="Unannounced Role")
        response = self.client.get(reverse("jobs:list"))
        self.assertNotContains(response, "Unannounced Role")

    def test_searching_narrows_the_list(self):
        publish(create_job(self.company, title="Product Designer"))

        response = self.client.get(reverse("jobs:list"), {"q": "python"})

        self.assertContains(response, "Senior Python Engineer")
        self.assertNotContains(response, "Product Designer")

    def test_an_htmx_request_returns_the_results_region_only(self):
        response = self.client.get(reverse("jobs:list"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        # The shell is not in a partial swap.
        self.assertNotContains(response, "<html")

    def test_a_filter_appears_in_the_applied_filter_bar(self):
        response = self.client.get(reverse("jobs:list"), {"workplace": "remote"})
        self.assertContains(response, "Filtered by")

    def test_the_detail_page_is_open_to_anyone(self):
        response = self.client.get(reverse("jobs:detail", args=[self.job.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Senior Python Engineer")

    def test_an_unpublished_posting_is_a_404_rather_than_a_403(self):
        draft = create_job(self.company)
        response = self.client.get(reverse("jobs:detail", args=[draft.slug]))
        self.assertEqual(response.status_code, 404)

    def test_a_suspended_company_takes_its_listings_with_it(self):
        self.company.state = CompanyState.SUSPENDED
        self.company.save(update_fields=["state"])

        self.assertEqual(
            self.client.get(reverse("jobs:detail", args=[self.job.slug])).status_code, 404
        )
        self.assertNotContains(self.client.get(reverse("jobs:list")), "Senior Python Engineer")

    def test_the_company_page_lists_open_roles(self):
        response = self.client.get(reverse("jobs:company", args=[self.company.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Senior Python Engineer")

    def test_a_company_page_for_a_suspended_company_is_a_404(self):
        self.company.state = CompanyState.SUSPENDED
        self.company.save(update_fields=["state"])
        response = self.client.get(reverse("jobs:company", args=[self.company.slug]))
        self.assertEqual(response.status_code, 404)


class ViewCountTests(TestCase):
    def setUp(self):
        self.company, _ = company_with_owner(create_user(email="owner@example.test"))
        self.job = publish(create_job(self.company))

    def test_opening_a_posting_counts_a_view(self):
        self.client.get(reverse("jobs:detail", args=[self.job.slug]))
        self.job.refresh_from_db()
        self.assertEqual(self.job.views_count, 1)

    def test_a_refresh_does_not_count_twice(self):
        self.client.get(reverse("jobs:detail", args=[self.job.slug]))
        self.client.get(reverse("jobs:detail", args=[self.job.slug]))

        self.job.refresh_from_db()
        self.assertEqual(self.job.views_count, 1)
        self.assertEqual(JobView.objects.count(), 1)


class SaveTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.company, _ = company_with_owner(create_user(email="owner@example.test"))
        self.job = publish(create_job(self.company))

    def test_next_on_another_site_is_ignored(self):
        # `next` was passed straight to redirect(): a crafted form could send a
        # candidate from a job page to a look-alike site.
        self.client.force_login(self.user)
        for target in ("https://evil.example/login", "//evil.example/", "javascript:alert(1)"):
            with self.subTest(target=target):
                response = self.client.post(
                    reverse("jobs:act"), {"command": "save", "slug": self.job.slug, "next": target}
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], self.job.get_absolute_url())

    def test_next_on_this_site_is_followed(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("jobs:act"), {"command": "save", "slug": self.job.slug, "next": "/jobs/saved/"}
        )
        self.assertEqual(response["Location"], "/jobs/saved/")

    def test_saving_requires_signing_in(self):
        response = self.client.post(reverse("jobs:act"), {"command": "save", "slug": self.job.slug})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SavedJob.objects.count(), 0)

    def test_one_button_toggles_from_the_record_rather_than_the_page(self):
        self.client.force_login(self.user)
        url = reverse("jobs:act")

        self.client.post(url, {"command": "save", "slug": self.job.slug})
        self.assertEqual(SavedJob.objects.count(), 1)

        # A stale tab posting "save" again must not create a second row, and
        # the server decides the direction, so the second press unsaves.
        self.client.post(url, {"command": "save", "slug": self.job.slug})
        self.assertEqual(SavedJob.objects.count(), 0)

    def test_an_htmx_save_returns_the_redrawn_control(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("jobs:act"),
            {"command": "save", "slug": self.job.slug, "render": "control"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"save-{self.job.slug}")

    def test_saving_a_posting_that_is_gone_is_a_404(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("jobs:act"), {"command": "save", "slug": "no-such-job"})
        self.assertEqual(response.status_code, 404)

    def test_the_saved_screen_lists_what_was_kept(self):
        self.client.force_login(self.user)
        self.client.post(reverse("jobs:act"), {"command": "save", "slug": self.job.slug})

        response = self.client.get(reverse("jobs:saved"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.job.title)


class SavedSearchScreenTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.client.force_login(self.user)

    def test_saving_a_search_stores_its_filters(self):
        self.client.post(
            reverse("jobs:act"),
            {
                "command": "save_search",
                "name": "Remote Python",
                "q": "python",
                "workplace": "remote",
                "alert_frequency": AlertFrequency.DAILY,
            },
        )

        record = SavedSearch.objects.get()
        self.assertEqual(record.query, {"q": "python", "workplace": "remote"})
        self.assertEqual(record.alert_frequency, AlertFrequency.DAILY)

    def test_another_persons_saved_search_cannot_be_deleted(self):
        stranger = create_profile(user=create_user(email="stranger@example.test"))
        theirs = SavedSearch.objects.create(profile=stranger, name="Theirs", query={"q": "python"})

        self.client.post(reverse("jobs:act"), {"command": "delete_search", "search_id": theirs.pk})

        self.assertTrue(SavedSearch.objects.filter(pk=theirs.pk).exists())

    def test_an_empty_search_is_refused(self):
        self.client.post(reverse("jobs:act"), {"command": "save_search", "name": "Everything"})
        self.assertEqual(SavedSearch.objects.count(), 0)


class DraftLetterFromJobPageTests(TestCase):
    """The job page's "Draft a letter" form names the job it is for (whole-site loop)."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def test_the_form_carries_the_job_slug(self):
        from apps.candidates.tests.factories import create_profile
        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.tests.factories import create_job, publish

        company, _ = company_with_owner(create_profile().user)
        job = publish(create_job(company, title="Data Engineer"))
        self.client.force_login(create_profile().user)

        page = self.client.get(reverse("jobs:detail", args=[job.slug])).content.decode()

        self.assertIn('name="command" value="draft"', page)
        self.assertIn(f'name="job" value="{job.slug}"', page)
