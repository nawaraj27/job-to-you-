"""
Staff impersonation (SRS FR-1.10): a recorded reason, a 60-minute expiry, no
staff-on-staff use, and an `on_behalf_of` stamp on every action performed.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts import impersonation
from apps.accounts.models import StaffRole
from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.candidates.tests.factories import create_profile


class ImpersonationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.agent = create_user(email="agent@careerai.example")
        StaffRole.objects.create(user=self.agent, level="support")
        self.person = create_profile().user
        self.person_url = reverse("staff:person", args=[self.person.pk])
        self.client.force_login(self.agent)

    def start(self, reason="Ticket 4411: cannot see their application"):
        return self.client.post(self.person_url, {"action": "impersonate", "reason": reason})

    def test_starting_needs_a_reason(self):
        self.start(reason="  ")

        response = self.client.get(reverse("dashboard:index"))
        self.assertIsNone(response.context["impersonation"])

    def test_the_banner_shows_on_every_page_and_the_person_is_who_the_page_is_for(self):
        self.start()

        page = self.client.get(reverse("settings:index"))

        self.assertContains(page, f"Viewing as {self.person.email}")
        self.assertEqual(page.context["user"], self.person)
        self.assertTrue(
            AuditLog.objects.filter(
                verb="account.impersonation_started", actor=self.agent, on_behalf_of=self.person
            ).exists()
        )

    def test_actions_are_stamped_with_both_people(self):
        self.start()

        self.client.post(
            reverse("settings:privacy_act"), {"command": "visibility", "value": "nobody"}
        )

        entry = AuditLog.objects.exclude(verb__startswith="account.impersonation").latest(
            "created_at"
        )
        self.assertEqual(entry.actor, self.agent)
        self.assertEqual(entry.on_behalf_of, self.person)

    def test_security_settings_data_and_staff_screens_are_closed(self):
        self.start()

        for url in (
            reverse("settings:security"),
            reverse("settings:data"),
            reverse("staff:operations"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("dashboard:index"))

    def test_stopping_returns_the_staff_member_to_themselves(self):
        self.start()

        self.client.post(reverse("staff:impersonation_stop"))

        page = self.client.get(reverse("staff:operations"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["user"], self.agent)
        self.assertTrue(AuditLog.objects.filter(verb="account.impersonation_ended").exists())

    def test_it_expires_after_sixty_minutes(self):
        self.start()
        session = self.client.session
        record = session[impersonation.SESSION_KEY]
        record["expires"] = record["expires"] - dt.timedelta(minutes=61).total_seconds()
        session[impersonation.SESSION_KEY] = record
        session.save()

        page = self.client.get(reverse("dashboard:index"))

        self.assertIsNone(page.context["impersonation"])
        self.assertEqual(page.context["user"], self.agent)
        entry = AuditLog.objects.get(verb="account.impersonation_ended")
        self.assertTrue(entry.metadata["expired"])

    def test_staff_cannot_impersonate_staff(self):
        colleague = create_user()
        StaffRole.objects.create(user=colleague, level="moderator")

        self.client.post(
            reverse("staff:person", args=[colleague.pk]), {"action": "impersonate", "reason": "x"}
        )

        self.assertNotIn(impersonation.SESSION_KEY, self.client.session)

    def test_a_non_staff_user_cannot_start_one(self):
        other = self.client_class()
        other.force_login(create_user())

        response = other.post(self.person_url, {"action": "impersonate", "reason": "x"})

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(impersonation.SESSION_KEY, other.session)

    def test_a_record_left_in_a_session_does_not_carry_to_another_account(self):
        self.start()
        session = self.client.session
        record = session[impersonation.SESSION_KEY]
        stranger = create_user()
        record["staff_id"] = stranger.pk
        session[impersonation.SESSION_KEY] = record
        session.save()

        page = self.client.get(reverse("dashboard:index"))

        self.assertEqual(page.context["user"], self.agent)
