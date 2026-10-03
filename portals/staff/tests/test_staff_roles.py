"""
Granting platform staff roles (SRS §2.3: Super Admin grants roles, "granting a
staff role requires approval by a second superadmin").

Before the whole-site loop there was no way to grant a staff role at all — no
admin registration, no service, no screen — so the only staff account was the
one a seed script wrote directly.
"""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import StaffRole, StaffRoleRequest
from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog


def superadmin():
    user = create_user()
    StaffRole.objects.create(user=user, level="superadmin")
    return user


class StaffRoleGrantTests(TestCase):
    def setUp(self):
        self.first = superadmin()
        self.second = superadmin()
        self.person = create_user()
        self.url = reverse("staff:team")

    def propose(self, by, **data):
        self.client.force_login(by)
        return self.client.post(
            self.url,
            {
                "action": "propose",
                "email": self.person.email,
                "level": "moderator",
                "reason": "On call",
                **data,
            },
        )

    def test_a_grant_needs_a_second_superadmin(self):
        self.propose(self.first)
        proposal = StaffRoleRequest.objects.get(user=self.person)
        self.assertFalse(StaffRole.objects.filter(user=self.person).exists())

        # The proposer cannot approve their own proposal.
        self.client.post(self.url, {"action": "approve", "request": proposal.pk})
        self.assertFalse(StaffRole.objects.filter(user=self.person).exists())

        self.client.force_login(self.second)
        self.client.post(self.url, {"action": "approve", "request": proposal.pk})

        role = StaffRole.objects.get(user=self.person)
        self.assertEqual(role.level, "moderator")
        self.assertEqual(role.granted_by, self.first)
        self.assertTrue(
            AuditLog.objects.filter(verb="staff.role_granted", actor=self.second).exists()
        )

    def test_nobody_proposes_a_role_for_themselves(self):
        self.client.force_login(self.first)
        self.client.post(
            self.url,
            {"action": "propose", "email": self.first.email, "level": "support", "reason": "x"},
        )

        self.assertFalse(StaffRoleRequest.objects.exists())

    def test_one_open_proposal_per_person(self):
        self.propose(self.first)
        self.propose(self.second, level="admin")

        self.assertEqual(StaffRoleRequest.objects.filter(user=self.person).count(), 1)

    def test_a_declined_proposal_grants_nothing(self):
        self.propose(self.first)
        proposal = StaffRoleRequest.objects.get(user=self.person)
        self.client.force_login(self.second)

        self.client.post(self.url, {"action": "decline", "request": proposal.pk})

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, "declined")
        self.assertFalse(StaffRole.objects.filter(user=self.person).exists())

    def test_revoking_is_immediate_and_signs_them_out(self):
        StaffRole.objects.create(user=self.person, level="support")
        theirs = self.client_class()
        theirs.force_login(self.person)
        self.assertEqual(theirs.get(reverse("staff:operations")).status_code, 200)

        self.client.force_login(self.first)
        self.client.post(
            self.url, {"action": "revoke", "user": self.person.pk, "reason": "Left the team"}
        )

        self.assertFalse(StaffRole.objects.filter(user=self.person).exists())
        self.assertNotEqual(theirs.get(reverse("staff:operations")).status_code, 200)

    def test_only_superadmins_open_the_screen(self):
        for level in ("support", "moderator", "admin"):
            with self.subTest(level=level):
                user = create_user()
                StaffRole.objects.create(user=user, level=level)
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.client.post(
                    self.url,
                    {
                        "action": "propose",
                        "email": self.person.email,
                        "level": "superadmin",
                        "reason": "x",
                    },
                )
        self.assertFalse(StaffRoleRequest.objects.exists())

    def test_tabs_only_offer_screens_the_level_can_open(self):
        support = create_user()
        StaffRole.objects.create(user=support, level="support")
        self.client.force_login(support)

        labels = [
            label
            for label, _url, _icon in self.client.get(reverse("staff:operations")).context["tabs"]
        ]

        self.assertNotIn("Verification", labels)
        self.assertNotIn("Staff", labels)
        self.assertIn("Operations", labels)


class BootstrapTests(TestCase):
    def test_the_first_superadmin_can_be_bootstrapped_once(self):
        user = create_user(email="first@example.com")

        call_command("bootstrap_superadmin", "first@example.com", stdout=_Null())

        self.assertEqual(StaffRole.objects.get(user=user).level, "superadmin")
        create_user(email="second@example.com")
        with self.assertRaises(CommandError):
            call_command("bootstrap_superadmin", "second@example.com", stdout=_Null())


class _Null:
    def write(self, *_args, **_kwargs):
        return None
