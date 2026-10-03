"""
Decision D-09: notification preferences stay on the account; the screens
around them follow who the person is.

One `NotificationPreference` set per user, never per company. The settings
pages are drawn in the side the person works on: an employer with no candidate
profile gets the employer rail for their company (or the company chooser, with
several), never the candidate rail, and opening settings no longer turns them
into a candidate by creating an empty profile. Notification settings are linked
from the employer rail and from the account menu.
"""

from __future__ import annotations

import re

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.candidates.models import CandidateProfile
from apps.candidates.tests.factories import create_profile
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner, create_company
from apps.notifications.models import NotificationPreference

CANDIDATE_ONLY = ("CV builder", "Matched to me", "Cover letters", "Your numbers")


def rail(html: str) -> tuple[str, list[str]]:
    label = re.search(r'<nav class="app-rail" aria-label="([^"]+)"', html)
    links = re.findall(r'class="rail-link"[^>]*>.*?<span class="truncate">([^<]+)', html, re.S)
    return (label.group(1) if label else ""), links


class EmployerOnlySettingsTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner, slug="d09-co", name="D09 Co")
        self.client.force_login(self.owner)

    def test_the_notification_settings_use_the_employer_rail_for_their_company(self):
        page = self.client.get(reverse("settings:notifications"))

        label, links = rail(page.content.decode())
        self.assertEqual(label, "Employer sections")
        for candidate_item in CANDIDATE_ONLY:
            self.assertNotIn(candidate_item, links)
        self.assertIn("Notification settings", links)
        # The way back to the company.
        self.assertContains(page, reverse("employer:overview", args=["d09-co"]))

    def test_no_settings_screen_creates_a_candidate_profile(self):
        for name in (
            "settings:index",
            "settings:notifications",
            "settings:privacy",
            "settings:data",
            "settings:appearance",
            "settings:security",
        ):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertFalse(CandidateProfile.objects.filter(user=self.owner).exists())

    def test_the_privacy_screen_shows_the_employer_version(self):
        page = self.client.get(reverse("settings:privacy"))
        self.assertContains(page, "What candidates see")
        self.assertNotContains(page, "Who can see your profile")

    def test_the_account_level_switch_still_works_and_candidate_commands_do_not(self):
        act = reverse("settings:privacy_act")
        response = self.client.post(act, {"command": "toggle", "key": "share_analytics"})
        self.assertEqual(response.status_code, 200)

        response = self.client.post(act, {"command": "visibility", "value": "nobody"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CandidateProfile.objects.filter(user=self.owner).exists())

    def test_the_data_screen_offers_no_profile_erasure_without_a_profile(self):
        page = self.client.get(reverse("settings:data"))
        self.assertNotContains(page, "Erase your profile")
        self.assertContains(page, "Close this account")
        response = self.client.post(
            reverse("settings:data_act"), {"command": "delete", "confirm": "DELETE"}
        )
        self.assertEqual(response.status_code, 400)

    def test_preferences_are_one_set_for_the_account(self):
        other, _ = company_with_owner(create_user(), slug="d09-other")
        add_member(other, self.owner, Role.RECRUITER)

        self.client.post(
            reverse("settings:notifications_act"),
            {
                "command": "channel",
                "event": "application_updates",
                "channel": "email",
                "on": "false",
            },
        )

        rows = NotificationPreference.objects.filter(
            user=self.owner, event_key="application_updates"
        )
        self.assertEqual(rows.count(), 1)
        self.assertFalse(rows.get().email)
        self.assertFalse(
            any(field.name == "company" for field in NotificationPreference._meta.get_fields())
        )

    def test_the_employer_rail_links_notification_settings(self):
        page = self.client.get(reverse("employer:overview", args=["d09-co"]))
        _label, links = rail(page.content.decode())
        self.assertIn("Notification settings", links)
        self.assertContains(page, reverse("settings:notifications"))

    def test_the_account_menu_links_notification_settings(self):
        page = self.client.get(reverse("employer:overview", args=["d09-co"]))
        menu = page.content.decode().split('id="account-menu"', 1)[1]
        self.assertIn(reverse("settings:notifications"), menu)
        self.assertIn("Notification settings", menu)


class ManyCompaniesSettingsTests(TestCase):
    def test_somebody_in_two_companies_gets_the_company_chooser(self):
        user = create_user()
        company_with_owner(user, slug="first-d09")
        add_member(create_company(slug="second-d09"), user, Role.RECRUITER)
        self.client.force_login(user)

        page = self.client.get(reverse("settings:notifications"))

        label, links = rail(page.content.decode())
        self.assertEqual(label, "Employer sections")
        self.assertIn("Your companies", links)
        self.assertContains(page, reverse("employer:index"))
        for candidate_item in CANDIDATE_ONLY:
            self.assertNotIn(candidate_item, links)


class CandidateSettingsTests(TestCase):
    def test_a_candidate_keeps_the_candidate_rail(self):
        profile = create_profile()
        self.client.force_login(profile.user)

        label, links = rail(self.client.get(reverse("settings:notifications")).content.decode())

        self.assertEqual(label, "Candidate sections")
        self.assertIn("CV builder", links)

    def test_somebody_who_is_both_follows_the_side_they_signed_up_for(self):
        profile = create_profile()
        company_with_owner(profile.user, slug="both-d09")
        self.client.force_login(profile.user)

        label, _links = rail(self.client.get(reverse("settings:notifications")).content.decode())
        self.assertEqual(label, "Candidate sections")

        profile.user.signup_intent = type(profile.user).SignupIntent.EMPLOYER
        profile.user.save(update_fields=["signup_intent"])
        label, links = rail(self.client.get(reverse("settings:notifications")).content.decode())
        self.assertEqual(label, "Employer sections")
        self.assertIn("Notification settings", links)

    def test_a_new_candidate_still_gets_a_profile_on_first_visit(self):
        user = create_user()
        self.client.force_login(user)
        self.client.get(reverse("settings:index"))
        self.assertTrue(CandidateProfile.objects.filter(user=user).exists())
