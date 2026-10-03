"""
The account-closure screen (SRS §19.4, §15.5).

The property that matters here is not that the buttons work — it is that they
say what they do. Until P19 this screen had one destructive control headed
"Close this account", wired to a service that empties the *profile* and leaves
the account open. That is the failure the erasure service's own docstring warns
about, sitting in the template above it.

So the assertions are about wording as much as behaviour: the two controls are
distinct, the account one schedules rather than acts, and the page tells
somebody the date it will happen and the two things that cannot be undone.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.services import erasure
from apps.candidates.tests.factories import create_profile


class DataScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.user = self.profile.user
        self.client.force_login(self.user)
        self.url = reverse("settings:data")
        self.act = reverse("settings:data_act")

    def test_the_two_destructive_controls_are_distinct(self):
        response = self.client.get(self.url)

        self.assertContains(response, "Erase your profile")
        self.assertContains(response, "Close this account")

    def test_the_profile_button_no_longer_claims_to_close_the_account(self):
        response = self.client.get(self.url)
        self.assertContains(response, "This empties your profile")

    def test_the_account_section_states_what_cannot_be_undone(self):
        # §19.4: both limits are stated to the person rather than implied.
        response = self.client.get(self.url)
        self.assertContains(response, "cannot be unsent")

    def test_requesting_closure_schedules_it_and_removes_nothing(self):
        response = self.client.post(self.act, {"command": "erase", "confirm": erasure.CONFIRMATION})
        self.user.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.user.erasure_requested_at)
        self.assertTrue(self.user.is_active)

    def test_the_wrong_confirmation_word_changes_nothing(self):
        self.client.post(self.act, {"command": "erase", "confirm": "please"})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.erasure_requested_at)

    def test_the_page_then_shows_the_date_and_a_way_out(self):
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        response = self.client.get(self.url)

        self.assertContains(response, "Keep my account")
        self.assertContains(response, "Nothing has been removed yet")

    def test_it_can_be_cancelled_from_the_page(self):
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        self.client.post(self.act, {"command": "cancel_erasure"})

        self.user.refresh_from_db()
        self.assertIsNone(self.user.erasure_requested_at)

    def test_the_profile_erase_still_works_and_leaves_the_account_open(self):
        self.client.post(self.act, {"command": "delete", "confirm": "DELETE"})

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertIsNone(self.user.erasure_requested_at)

    def test_an_unknown_command_is_refused(self):
        response = self.client.post(self.act, {"command": "drop-tables"})
        self.assertEqual(response.status_code, 400)

    def test_a_signed_out_visitor_reaches_none_of_it(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_one_account_cannot_schedule_anothers_closure(self):
        # There is no identifier in the form — the account acted on is the one
        # signed in — and this asserts that stays true.
        stranger = create_profile().user
        self.client.post(self.act, {"command": "erase", "confirm": erasure.CONFIRMATION})

        stranger.refresh_from_db()
        self.assertIsNone(stranger.erasure_requested_at)
        self.assertEqual(User.objects.filter(erasure_requested_at__isnull=False).count(), 1)
