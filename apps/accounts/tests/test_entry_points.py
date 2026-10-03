"""
Where each kind of account lands, and what the account menu offers it
(SRS §2.1: identity, membership and staff role are independent; "both can be
true"). Found by the whole-site loop's persona sweep.
"""

from __future__ import annotations

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import StaffRole
from apps.accounts.tests.factories import create_user
from apps.candidates.tests.factories import create_profile
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner

PASSWORD = "test-password-not-a-secret"


class EntryPointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def sign_in(self, user):
        cache.clear()
        return self.client.post(
            reverse("accounts:login"), {"username": user.email, "password": PASSWORD}
        )

    def menu(self):
        return self.client.get(reverse("dashboard:index")).content.decode()

    def test_a_candidate_who_joins_a_company_is_offered_both_sides(self):
        person = create_profile().user  # signed up as a candidate
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        add_member(company, person, role=Role.RECRUITER)
        self.client.force_login(person)

        page = self.menu()

        self.assertIn(reverse("dashboard:profile"), page)
        self.assertIn(reverse("employer:index"), page)
        self.assertIn(">Hiring<", page)

    def test_somebody_who_came_to_hire_still_reaches_the_candidate_side(self):
        person = create_user(signup_intent="employer")
        self.client.force_login(person)

        page = self.menu()

        self.assertIn(reverse("dashboard:profile"), page)
        self.assertIn("Set up a company", page)

    def test_platform_staff_are_offered_the_staff_area_and_nobody_else_is(self):
        member = create_user()
        StaffRole.objects.create(user=member, level="support")
        self.client.force_login(member)
        self.assertIn(reverse("staff:index"), self.menu())

        self.client.force_login(create_user())
        self.assertNotIn(reverse("staff:index"), self.menu())

    def test_staff_land_in_the_staff_area(self):
        member = create_user()
        StaffRole.objects.create(user=member, level="support")

        response = self.sign_in(member)

        self.assertEqual(response["Location"], reverse("staff:index"))

    def test_sign_up_is_closed_to_somebody_already_signed_in(self):
        person = create_user()
        self.client.force_login(person)

        page = self.client.get(reverse("accounts:signup"))
        cache.clear()
        submitted = self.client.post(
            reverse("accounts:signup"),
            {
                "full_name": "Second Account",
                "email": "second@example.com",
                "signup_intent": "candidate",
                "password1": "Another-Pass-9911",
                "password2": "Another-Pass-9911",
            },
        )

        self.assertEqual(page.status_code, 302)
        self.assertEqual(submitted.status_code, 302)
        from apps.accounts.models import User

        self.assertFalse(User.objects.filter(email="second@example.com").exists())
        self.assertEqual(self.client.get(reverse("settings:index")).context["user"], person)
