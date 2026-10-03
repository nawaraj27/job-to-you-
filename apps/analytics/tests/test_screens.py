"""
The analytics screens (SRS §16.2).

Two properties worth asserting through the screen rather than only the service:

  * **the employer page reads rollups**, so a company with raw activity and no
    rollup yet sees an honest "nothing to report" rather than a wrong number;
  * **one company's numbers never appear on another's page** — the same tenant
    rule as everywhere, asserted from the failing side.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import CompanyDailyStat, FunnelSnapshot, JobDailyStat
from apps.analytics.services import reports
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for


class AnalyticsScreenFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.today = timezone.localdate()

        self.client.force_login(self.owner)
        self.url = reverse("employer:analytics", args=[self.company.slug])

    def make_rollup(self, **overrides):
        fields = {
            "company": self.company,
            "day": self.today,
            "applications": 20,
            "responded": 14,
            "response_hours_total": Decimal("140"),
            "interviews": 4,
            "offers": 2,
            "hires": 1,
        }
        fields.update(overrides)
        return CompanyDailyStat.objects.create(**fields)


class EmployerScreenTests(AnalyticsScreenFixture):
    def test_the_screen_renders(self):
        self.make_rollup()
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Applications")

    def test_a_company_with_no_rollups_is_told_so(self):
        # Honest rather than a page of zeroes that reads like a failing company.
        response = self.client.get(self.url)
        self.assertContains(response, "Nothing to report yet")

    def test_the_headline_numbers_come_from_the_rollup(self):
        self.make_rollup(applications=37)
        response = self.client.get(self.url)
        self.assertContains(response, "37")

    def test_the_response_nudge_appears_for_a_poor_rate(self):
        self.make_rollup(applications=100, responded=20, response_hours_total=Decimal("200"))
        response = self.client.get(self.url)
        self.assertContains(response, "worst part of applying")

    def test_no_nudge_for_a_company_doing_well(self):
        self.make_rollup(applications=100, responded=90, response_hours_total=Decimal("180"))
        response = self.client.get(self.url)
        self.assertNotContains(response, "worst part of applying")

    def test_the_funnel_renders_when_there_is_one(self):
        self.make_rollup()
        FunnelSnapshot.objects.create(
            company=self.company, day=self.today, stage_key="new", stage_order=10, count=12
        )
        response = self.client.get(self.url)
        self.assertContains(response, "Where everybody is")

    def test_posting_performance_renders(self):
        self.make_rollup()
        JobDailyStat.objects.create(
            job=self.job, day=self.today, views=100, applications=10, shortlisted=3
        )
        response = self.client.get(self.url)
        self.assertContains(response, self.job.title)

    def test_the_window_can_be_narrowed(self):
        self.make_rollup()
        response = self.client.get(self.url, {"days": "7"})
        self.assertEqual(response.context["days"], 7)

    def test_an_unknown_window_falls_back(self):
        # A hand-typed query string must not ask for five years of rollups.
        self.make_rollup()
        response = self.client.get(self.url, {"days": "9999"})
        self.assertEqual(response.context["days"], 30)

    def test_another_companys_numbers_are_not_visible(self):
        self.make_rollup(applications=999)
        stranger = create_profile().user
        other_company, _ = company_with_owner(stranger)
        self.client.force_login(stranger)

        response = self.client.get(reverse("employer:analytics", args=[other_company.slug]))
        self.assertNotContains(response, "999")

    def test_signing_out_closes_the_screen(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class CandidateScreenTests(AnalyticsScreenFixture):
    def setUp(self):
        super().setUp()
        self.candidate = create_profile()
        self.client.force_login(self.candidate.user)
        self.url = reverse("dashboard:insights")

    def test_a_candidate_with_nothing_is_told_so(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Nothing to show yet")

    def test_applications_appear(self):
        application_for(self.candidate, self.job)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Applications")

    def test_the_response_view_reframes_silence(self):
        # "This is about them, not you" is the most useful thing the page says.
        application_for(self.candidate, self.job)
        response = self.client.get(self.url)
        self.assertContains(response, "Are you being ignored?")

    def test_another_candidates_numbers_are_not_shown(self):
        stranger = create_profile()
        application_for(stranger, self.job)

        response = self.client.get(self.url)
        self.assertEqual(response.context["insights"]["applications"], 0)


class ReportTests(AnalyticsScreenFixture):
    """The report functions, where the screens do not reach."""

    def test_an_empty_company_overview_is_zeroes_and_says_so(self):
        overview = reports.company_overview(self.company)
        self.assertFalse(overview["has_data"])
        self.assertEqual(overview["applications"], 0)

    def test_the_overview_says_how_current_it_is(self):
        """ "Updated overnight" is a promise about a job that may not have run.

        An employer reading "0 interviews" on the same portal that shows one
        booked for Tuesday needs to know which figure is older, and the answer
        is a date.
        """
        CompanyDailyStat.objects.create(
            company=self.company,
            day=timezone.localdate() - dt.timedelta(days=2),
            applications=5,
            responded=2,
        )

        overview = reports.company_overview(self.company)

        self.assertEqual(overview["counted_to"], timezone.localdate() - dt.timedelta(days=2))
        self.assertIsNotNone(overview["computed_at"])

    def test_an_overview_with_nothing_counted_says_so_rather_than_a_date(self):
        overview = reports.company_overview(self.company)

        self.assertIsNone(overview["counted_to"])

    def test_the_screen_prints_the_date_it_was_counted_to(self):
        CompanyDailyStat.objects.create(
            company=self.company,
            day=timezone.localdate() - dt.timedelta(days=1),
            applications=3,
            responded=1,
        )

        response = self.client.get(reverse("employer:analytics", args=[self.company.slug]))

        self.assertContains(response, "Counted up to")
        self.assertNotContains(response, "Updated overnight")

    def test_the_funnel_is_empty_without_a_snapshot(self):
        self.assertEqual(reports.company_funnel(self.company), [])

    def test_drop_off_is_measured_against_the_previous_stage(self):
        for order, (key, count) in enumerate([("new", 100), ("reviewing", 40), ("interview", 10)]):
            FunnelSnapshot.objects.create(
                company=self.company,
                day=self.today,
                stage_key=key,
                stage_order=order * 10,
                count=count,
            )

        funnel = reports.company_funnel(self.company)
        self.assertIsNone(funnel[0]["drop_off"])
        self.assertEqual(funnel[1]["drop_off"], 0.6)
        self.assertEqual(funnel[2]["drop_off"], 0.75)

    def test_a_stage_somebody_skipped_into_is_not_a_negative_drop_off(self):
        """A recruiter may move somebody straight past a stage.

        The funnel then holds more people at `interview` than at `reviewing`,
        and the drop-off arithmetic produced "-100% do not reach this stage" —
        a sentence about nothing, on the screen a hiring manager is meant to
        act on.
        """
        for order, (key, count) in enumerate([("new", 4), ("reviewing", 1), ("interview", 2)]):
            FunnelSnapshot.objects.create(
                company=self.company,
                day=self.today,
                stage_key=key,
                stage_order=order * 10,
                count=count,
            )

        funnel = reports.company_funnel(self.company)

        self.assertEqual(funnel[1]["drop_off"], 0.75)
        self.assertIsNone(funnel[2]["drop_off"])
        self.assertEqual(funnel[2]["skipped_in"], 1)

    def test_time_to_hire_reports_no_data_honestly(self):
        result = reports.time_to_hire(self.company)
        self.assertFalse(result["has_data"])

    def test_time_to_hire_is_measured_end_to_end(self):
        from apps.applications.models import Application
        from apps.applications.services import pipeline

        application = application_for(create_profile(), self.job)
        Application.objects.filter(pk=application.pk).update(
            applied_at=timezone.now() - dt.timedelta(days=20),
            stage=pipeline.stage_by_key(self.company, "hired"),
            last_activity_at=timezone.now(),
        )

        result = reports.time_to_hire(self.company)
        self.assertTrue(result["has_data"])
        self.assertEqual(result["days"], 20)

    def test_ai_cost_is_grouped_by_task_and_ordered_by_spend(self):
        from apps.analytics.models import AICostDaily

        AICostDaily.objects.create(
            day=self.today, task_key="cheap.task", runs=10, cost=Decimal("0.01")
        )
        AICostDaily.objects.create(
            day=self.today, task_key="dear.task", runs=2, cost=Decimal("5.00")
        )

        result = reports.ai_cost()
        self.assertEqual(result["tasks"][0]["task"], "dear.task")
        self.assertEqual(result["total_runs"], 12)

    def test_ai_cost_reports_nothing_honestly(self):
        self.assertFalse(reports.ai_cost()["has_data"])
