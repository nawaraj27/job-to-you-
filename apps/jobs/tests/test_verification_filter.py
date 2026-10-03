"""
The job list's company verification filter (docs/Job-Search.md).

  * All companies by default; verified-only and not-yet-verified on request.
  * Read from the company's verification record, so no request parameter can
    make a company verified.
  * Works with text search, other filters, sorting, pagination, saved
    searches and the facet counts.
  * Every row says which kind of company it is.
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.companies.models import Company, VerificationState
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import RemoteScope, Workplace
from apps.jobs.search import CompanyVerification, SearchQueryInput, search
from apps.jobs.tests.factories import create_job, publish


class VerificationFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.verified, _ = company_with_owner(create_user(), name="Himal Health")
        Company.objects.filter(pk=cls.verified.pk).update(
            verification_state=VerificationState.VERIFIED
        )
        cls.pending, _ = company_with_owner(create_user(), name="Kathmandu Kitchens")
        Company.objects.filter(pk=cls.pending.pk).update(
            verification_state=VerificationState.PENDING
        )
        cls.v_jobs = [
            publish(create_job(cls.verified, title=f"Staff Nurse {i}", workplace=Workplace.ON_SITE))
            for i in range(23)
        ]
        cls.u_jobs = [
            publish(create_job(cls.pending, title=f"Line Cook {i}", workplace=Workplace.ON_SITE))
            for i in range(12)
        ]
        publish(
            create_job(
                cls.verified,
                title="Remote Nurse Educator",
                workplace=Workplace.REMOTE,
                remote_scope=RemoteScope.COUNTRY,
            )
        )

    def setUp(self):
        cache.clear()

    def run_search(self, **data):
        return search(SearchQueryInput.from_mapping(data))

    def companies(self, result) -> set[str]:
        return {job.company.name for job in result.jobs}

    def test_all_companies_by_default(self):
        result = self.run_search()
        self.assertEqual(self.companies(result), {"Himal Health", "Kathmandu Kitchens"})
        self.assertEqual(result.total, 36)

    def test_verified_only(self):
        result = self.run_search(verification="verified")
        self.assertEqual(self.companies(result), {"Himal Health"})
        self.assertEqual(result.total, 24)

    def test_not_yet_verified(self):
        result = self.run_search(verification="unverified")
        self.assertEqual(self.companies(result), {"Kathmandu Kitchens"})
        self.assertEqual(result.total, 12)

    def test_a_tampered_parameter_is_ignored_not_trusted(self):
        for value in ("VERIFIED", "1", "true", "yes", "verified'--"):
            with self.subTest(value=value):
                result = self.run_search(verification=value)
                self.assertEqual(result.total, 36)
        # Nor can the company be presented as verified by any other field.
        page = self.client.get(reverse("jobs:list"), {"verification": "verified", "verified": "1"})
        self.assertNotContains(page, "Kathmandu Kitchens")

    def test_combines_with_text_filters_and_sort(self):
        result = self.run_search(
            q="nurse", verification="verified", workplace="remote", sort="newest"
        )
        self.assertEqual([job.title for job in result.jobs], ["Remote Nurse Educator"])
        result = self.run_search(q="nurse", verification="unverified")
        self.assertEqual(result.total, 0)

    def test_facet_counts(self):
        facets = {row["value"]: row["count"] for row in self.run_search().facets["verification"]}
        self.assertEqual(facets, {"verified": 24, "unverified": 12})
        narrowed = {
            row["value"]: row["count"]
            for row in self.run_search(workplace="remote").facets["verification"]
        }
        self.assertEqual(narrowed, {"verified": 1})

    def test_pagination_keeps_the_filter(self):
        first = self.client.get(
            reverse("jobs:list"), {"verification": "verified", "sort": "newest"}
        )
        self.assertContains(first, "verification=verified")
        second = self.client.get(
            reverse("jobs:list"), {"verification": "verified", "sort": "newest", "page": 2}
        )
        self.assertNotContains(second, "Kathmandu Kitchens")
        self.assertContains(second, "Staff Nurse")
        titles_one = {
            j.title for j in self.run_search(verification="verified", sort="newest").jobs[:20]
        }
        titles_two = {
            j.title for j in self.run_search(verification="verified", sort="newest").jobs[20:]
        }
        self.assertFalse(titles_one & titles_two)
        self.assertEqual(len(titles_one | titles_two), 24)

    def test_the_other_choice_stays_on_offer_once_one_is_chosen(self):
        facets = {
            row["value"]: row["count"]
            for row in self.run_search(verification="verified").facets["verification"]
        }
        self.assertEqual(facets, {"verified": 24, "unverified": 12})

    def test_saved_searches_keep_it(self):
        query = SearchQueryInput.from_mapping({"verification": "unverified", "q": "cook"})
        self.assertEqual(query.as_mapping()["verification"], "unverified")
        again = SearchQueryInput.from_mapping(query.as_mapping())
        self.assertEqual(again.verification, CompanyVerification.UNVERIFIED)

    def test_the_rail_and_rows_say_which_is_which(self):
        page = self.client.get(reverse("jobs:list"))
        self.assertContains(page, "All companies")
        self.assertContains(page, "Verified companies")
        self.assertContains(page, "Not yet verified")
        self.assertContains(page, "Not verified")
        self.assertContains(page, ">Verified</span>", html=False)
        chosen = self.client.get(reverse("jobs:list"), {"verification": "unverified"})
        self.assertContains(chosen, "Not yet verified")  # the active filter chip
