"""
The talent screens (FR-9.1 to FR-9.4, FR-2.8).

Two properties worth stating about what is asserted here.

**Opening a profile is a GET that changes state**, which is normally a thing to
avoid. The read *is* the event — there is no honest way to record that somebody
read a profile without the profile being read — so the test asserts the record
appears, and that a refused read leaves nothing behind.

**Export is a POST** for the opposite reason: it creates a view row per
candidate, and a GET a refresh repeated would tell every one of them they were
read twice.
"""

from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal
from io import StringIO

from django.test import TestCase
from django.urls import reverse

from apps.candidates.models import (
    CandidateSkill,
    ProfileExperience,
    ProfileView,
    Visibility,
)
from apps.candidates.services import profile as profile_service
from apps.candidates.services import talent as talent_service
from apps.candidates.tests.factories import create_profile
from apps.candidates.tests.test_talent import discoverable, make_searchable
from apps.companies.models import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.taxonomy.models import Location, Skill


class TalentScreenFixture(TestCase):
    def setUp(self):
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)

        self.python = Skill.objects.create(name="Python", slug="python")
        self.kathmandu = Location.objects.create(country="NP", city="Kathmandu")

        self.candidate = discoverable(headline="Backend engineer, payments")
        self.candidate.location = self.kathmandu
        self.candidate.save(update_fields=["location", "updated_at"])
        CandidateSkill.objects.create(profile=self.candidate, skill=self.python, years=Decimal("4"))
        ProfileExperience.objects.create(
            profile=self.candidate,
            title="Backend Engineer",
            org_name="Nexa",
            start_date=dt.date(2021, 1, 1),
            end_date=dt.date(2024, 1, 1),
        )
        profile_service.recompute(self.candidate)
        make_searchable(self.candidate)

        self.client.force_login(self.owner)
        self.search_url = reverse("employer:talent", args=[self.company.slug])
        self.export_url = reverse("employer:talent_export", args=[self.company.slug])
        self.profile_url = reverse(
            "employer:talent_profile", args=[self.company.slug, self.candidate.public_id]
        )


class SearchScreenTests(TalentScreenFixture):
    def test_the_screen_renders_before_a_search(self):
        response = self.client.get(self.search_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Search by skill, place or the words on a profile")

    def test_a_search_returns_matching_candidates(self):
        response = self.client.get(self.search_url, {"q": "payments"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backend engineer, payments")

    def test_the_screen_says_that_opening_a_profile_is_visible(self):
        # A recruiter who knows the candidate can see them behaves the way the
        # product needs. It is the same fact the candidate is shown.
        response = self.client.get(self.search_url)
        self.assertContains(response, "recorded and shown to the candidate")

    def test_a_result_row_carries_no_contact_details(self):
        response = self.client.get(self.search_url, {"q": "payments"})

        self.assertNotContains(response, self.candidate.user.email)
        self.assertContains(response, "Python")

    def test_a_search_with_no_matches_says_so(self):
        response = self.client.get(self.search_url, {"q": "ornithology"})
        self.assertContains(response, "Nobody matched that")

    def test_an_htmx_request_returns_only_the_results(self):
        response = self.client.get(self.search_url, {"q": "payments"}, HTTP_HX_REQUEST="true")
        self.assertContains(response, "talent-results")
        self.assertNotContains(response, "<html")

    def test_somebody_without_the_search_grant_is_refused(self):
        stranger = create_profile().user
        add_member(self.company, stranger, Role.INTERVIEWER)
        self.client.force_login(stranger)

        response = self.client.get(self.search_url, {"q": "payments"})
        self.assertEqual(response.status_code, 403)

    def test_a_recruiter_sees_no_export_button(self):
        recruiter = create_profile().user
        add_member(self.company, recruiter, Role.RECRUITER)
        self.client.force_login(recruiter)

        response = self.client.get(self.search_url, {"q": "payments"})
        self.assertNotContains(response, "Export selected")

    def test_an_owner_sees_the_export_button(self):
        response = self.client.get(self.search_url, {"q": "payments"})
        self.assertContains(response, "Export selected")

    def test_signing_out_closes_the_screen(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.search_url).status_code, 302)


class ProfileScreenTests(TalentScreenFixture):
    def test_opening_a_profile_renders_it(self):
        response = self.client.get(self.profile_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backend engineer, payments")
        self.assertContains(response, "Nexa")

    def test_opening_a_profile_records_the_view(self):
        self.client.get(self.profile_url)
        self.assertEqual(ProfileView.objects.filter(candidate=self.candidate).count(), 1)

    def test_the_page_tells_the_recruiter_the_candidate_can_see_it(self):
        response = self.client.get(self.profile_url)
        self.assertContains(response, "can see that")

    def test_the_page_shows_no_contact_details(self):
        # ADR-A34: contact details are revealed at a stage that requires an
        # application, and finding somebody in a search is not that stage.
        response = self.client.get(self.profile_url)
        self.assertNotContains(response, self.candidate.user.email)
        self.assertContains(response, "not shown here")

    def test_a_hidden_candidate_is_a_404(self):
        hidden = discoverable(visibility=Visibility.NOBODY)
        url = reverse("employer:talent_profile", args=[self.company.slug, hidden.public_id])

        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_refused_read_records_nothing(self):
        hidden = discoverable(visibility=Visibility.NOBODY)
        url = reverse("employer:talent_profile", args=[self.company.slug, hidden.public_id])

        self.client.get(url)
        self.assertFalse(ProfileView.objects.filter(candidate=hidden).exists())

    def test_another_companys_recruiter_cannot_reach_it_through_this_company(self):
        stranger = create_profile().user
        other_company, _ = company_with_owner(stranger)
        self.client.force_login(stranger)

        response = self.client.get(self.profile_url)
        self.assertEqual(response.status_code, 404)


class ExportScreenTests(TalentScreenFixture):
    def test_an_export_downloads_a_csv(self):
        response = self.client.post(self.export_url, {"candidate": [str(self.candidate.public_id)]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment;", response["Content-Disposition"])

    def test_the_csv_carries_the_allow_listed_columns_only(self):
        response = self.client.post(self.export_url, {"candidate": [str(self.candidate.public_id)]})
        reader = csv.DictReader(StringIO(response.content.decode()))

        self.assertEqual(reader.fieldnames, list(talent_service.EXPORT_FIELDS))
        row = next(reader)
        self.assertEqual(row["public_id"], str(self.candidate.public_id))
        self.assertNotIn(self.candidate.user.email, response.content.decode())

    def test_exporting_tells_every_candidate(self):
        self.client.post(self.export_url, {"candidate": [str(self.candidate.public_id)]})
        self.assertEqual(ProfileView.objects.filter(candidate=self.candidate).count(), 1)

    def test_a_recruiter_cannot_export(self):
        recruiter = create_profile().user
        add_member(self.company, recruiter, Role.RECRUITER)
        self.client.force_login(recruiter)

        response = self.client.post(self.export_url, {"candidate": [str(self.candidate.public_id)]})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ProfileView.objects.exists())

    def test_an_empty_export_is_refused_with_a_message(self):
        response = self.client.post(self.export_url, {})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProfileView.objects.exists())


class WhoViewedMeScreenTests(TalentScreenFixture):
    """FR-2.8, from the candidate's side."""

    def setUp(self):
        super().setUp()
        self.visibility_url = reverse("applications:visibility")

    def test_the_candidate_sees_that_a_company_looked(self):
        self.client.get(self.profile_url)
        self.client.force_login(self.candidate.user)

        response = self.client.get(self.visibility_url)
        self.assertContains(response, self.company.name)
        self.assertContains(response, "From a candidate search")

    def test_repeat_reads_are_reported_once(self):
        for _ in range(3):
            self.client.get(self.profile_url)
        self.client.force_login(self.candidate.user)

        response = self.client.get(self.visibility_url)
        self.assertContains(response, "3 times")

    def test_the_recruiter_who_looked_is_not_named(self):
        self.client.get(self.profile_url)
        self.client.force_login(self.candidate.user)

        response = self.client.get(self.visibility_url)
        self.assertNotContains(response, self.owner.email)

    def test_a_candidate_nobody_looked_at_is_told_so_plainly(self):
        self.client.force_login(self.candidate.user)
        response = self.client.get(self.visibility_url)

        self.assertContains(response, "No employer has opened your profile yet")

    def test_another_candidate_cannot_see_these_views(self):
        self.client.get(self.profile_url)
        stranger = create_profile()
        self.client.force_login(stranger.user)

        response = self.client.get(self.visibility_url)
        self.assertNotContains(response, "From a candidate search")
