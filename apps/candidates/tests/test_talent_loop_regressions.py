"""
Talent search and privacy defects reproduced on the running product.

  * a company the candidate had blocked still found a public candidate in
    search, opened the profile, and got a fresh grant for doing it;
  * switching visibility from "anyone" to "employers you've applied to" left
    every company that had opened the profile able to find and read it;
  * a candidate's headline went into the export as a live spreadsheet formula;
  * the export's `profile_url` had no company in it and 404ed;
  * a malformed candidate id in the export form was a 500;
  * `min_months=-1` returned the whole corpus past the empty-search refusal;
  * the "who opened your profile" list showed one company twice on one day.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from io import StringIO

from django.urls import reverse

from apps.applications.models import CandidateVisibilityGrant, GrantBasis
from apps.applications.services import visibility as visibility_service
from apps.candidates.models import ProfileView, ViewSource, Visibility
from apps.candidates.search import TalentQuery
from apps.candidates.services import blocking
from apps.candidates.services import profile as profile_service
from apps.candidates.services import talent as talent_service
from apps.candidates.tests.test_talent import make_searchable
from apps.candidates.tests.test_talent_screens import TalentScreenFixture


class BlockedCompanyTests(TalentScreenFixture):
    def setUp(self):
        super().setUp()
        blocking.block_company(
            profile=self.candidate, actor=self.candidate.user, company=self.company
        )

    def test_a_blocked_company_does_not_find_a_public_candidate(self):
        response = self.client.get(self.search_url, {"q": "payments"})
        self.assertNotContains(response, str(self.candidate.public_id))

    def test_a_blocked_company_cannot_open_the_profile_or_earn_a_grant(self):
        self.assertEqual(self.client.get(self.profile_url).status_code, 404)
        self.assertFalse(
            CandidateVisibilityGrant.objects.live()
            .filter(candidate=self.candidate, company=self.company)
            .exists()
        )
        self.assertFalse(ProfileView.objects.filter(candidate=self.candidate).exists())


class NarrowedVisibilityTests(TalentScreenFixture):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.client.get(self.profile_url).status_code, 200)

    def narrow(self, to):
        profile_service.update_privacy(
            profile=self.candidate, actor=self.candidate.user, visibility=to
        )
        self.candidate.refresh_from_db()
        # Isolate the grant rule from the completeness rule.
        make_searchable(self.candidate)

    def test_employers_only_withdraws_access_earned_by_a_public_read(self):
        self.narrow(Visibility.EMPLOYERS)
        self.assertEqual(self.client.get(self.profile_url).status_code, 404)
        self.assertNotContains(
            self.client.get(self.search_url, {"q": "payments"}), str(self.candidate.public_id)
        )

    def test_a_grant_from_an_application_survives(self):
        visibility_service.grant(
            candidate=self.candidate, company=self.company, basis=GrantBasis.APPLICATION
        )
        self.narrow(Visibility.EMPLOYERS)
        self.assertEqual(self.client.get(self.profile_url).status_code, 200)

    def test_going_back_to_public_needs_a_new_read(self):
        self.narrow(Visibility.EMPLOYERS)
        self.narrow(Visibility.EVERYONE)
        self.assertEqual(
            CandidateVisibilityGrant.objects.live()
            .filter(candidate=self.candidate, basis=GrantBasis.PUBLIC)
            .count(),
            0,
        )


class ExportSafetyTests(TalentScreenFixture):
    def rows(self, ids):
        response = self.client.post(self.export_url, {"candidate": ids})
        self.assertEqual(response.status_code, 200)
        return list(csv.DictReader(StringIO(response.content.decode())))

    def test_a_formula_in_a_headline_is_exported_as_text(self):
        self.candidate.headline = '=HYPERLINK("http://evil.example","open")'
        self.candidate.save(update_fields=["headline", "updated_at"])

        row = self.rows([str(self.candidate.public_id)])[0]
        self.assertTrue(row["headline"].startswith("'="), row["headline"])

    def test_the_profile_link_opens_the_profile(self):
        row = self.rows([str(self.candidate.public_id)])[0]
        self.assertEqual(row["profile_url"], self.profile_url)
        self.assertEqual(self.client.get(row["profile_url"]).status_code, 200)

    def test_a_malformed_id_is_a_message_not_a_500(self):
        response = self.client.post(self.export_url, {"candidate": ["not-a-uuid"]})
        self.assertRedirects(response, self.search_url, fetch_redirect_response=False)

    def test_malformed_and_duplicate_ids_are_ignored(self):
        pid = str(self.candidate.public_id)
        self.assertEqual(len(self.rows(["nope", pid, pid])), 1)
        self.assertEqual(ProfileView.objects.filter(candidate=self.candidate).count(), 1)


class QueryParsingTests(TalentScreenFixture):
    def test_negative_experience_is_not_a_search(self):
        self.assertTrue(TalentQuery.from_mapping({"min_months": "-1"}).is_empty)
        response = self.client.get(self.search_url, {"min_months": "-1"})
        self.assertFalse(response.context["searched"])
        self.assertNotContains(response, str(self.candidate.public_id))

    def test_nonsense_salaries_are_absent(self):
        for value in ("NaN", "Infinity", "-5", "abc"):
            self.assertIsNone(TalentQuery.from_mapping({"max_salary": value}).max_salary_base)
        self.assertEqual(
            TalentQuery.from_mapping({"max_salary": "90000"}).max_salary_base, Decimal("90000")
        )


class WhoOpenedGroupingTests(TalentScreenFixture):
    def test_one_company_on_one_day_is_one_line_naming_both_routes(self):
        self.client.get(self.profile_url)
        ProfileView.objects.create(
            candidate=self.candidate,
            company=self.company,
            actor=self.owner,
            source=ViewSource.APPLICATION,
        )

        rows = talent_service.views_for(self.candidate)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["times"], 2)
        self.assertEqual(rows[0]["sources"], [ViewSource.APPLICATION, ViewSource.SEARCH])

        self.client.force_login(self.candidate.user)
        response = self.client.get(reverse("applications:visibility"))
        self.assertContains(response, "From your application and a candidate search")

    def test_the_screen_points_to_where_blocking_actually_is(self):
        self.client.get(self.profile_url)
        self.client.force_login(self.candidate.user)
        response = self.client.get(reverse("applications:visibility"))
        self.assertContains(response, reverse("messages:inbox"))
        self.assertNotContains(response, "Blocking a company in")
