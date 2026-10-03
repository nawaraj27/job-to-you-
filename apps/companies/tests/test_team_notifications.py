"""
Team membership changes reach the people they are about (Loop Closure EMP-02).

`company.member_joined`, `company.member_removed` and
`company.ownership_transferred` were emitted with no audience, so nobody was
told anything: a removed member's next click was a bare 404, an inviter never
learned the invitation was accepted, and an ownership transfer notified neither
side although SRS §2.6 says "both are notified".
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.companies.models import CompanyMembership
from apps.companies.permissions import Role
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member, company_with_owner
from apps.notifications.models import Notification
from apps.notifications.services import fanout, outbox


def deliver() -> None:
    for _ in range(10):
        if not outbox.drain(500):
            break
    for _ in range(10):
        if not fanout.run_due(limit=500).get("chunks"):
            break


class TeamNotificationTests(TestCase):
    def setUp(self):
        self.owner = create_user(full_name="Asha Owner")
        self.company, self.owner_membership = company_with_owner(self.owner, name="Team Notify Co")
        self.bystander = create_user(full_name="Bina Bystander")
        add_member(self.company, self.bystander, Role.RECRUITER)

    def recipients(self, kind):
        return set(Notification.objects.filter(kind=kind).values_list("recipient_id", flat=True))

    def test_the_inviter_is_told_when_the_invitation_is_accepted(self):
        invitee = create_user(email="new.member@example.test", full_name="Nabin New")
        invitation, token = membership_service.invite(
            actor=self.owner,
            actor_membership=self.owner_membership,
            company=self.company,
            email=invitee.email,
            role=Role.RECRUITER,
        )
        membership_service.accept_invite(token=token, user=invitee)
        deliver()

        self.assertEqual(self.recipients("company.member_joined"), {self.owner.pk})
        row = Notification.objects.get(kind="company.member_joined")
        self.assertEqual(row.title, "Nabin New joined Team Notify Co")
        self.assertIn("Recruiter", row.body)
        self.assertEqual(row.target_url, reverse("employer:team", args=[self.company.slug]))

    def test_the_removed_member_alone_is_told(self):
        target = CompanyMembership.objects.get(company=self.company, user=self.bystander)
        membership_service.remove_member(
            actor=self.owner, actor_membership=self.owner_membership, target=target
        )
        deliver()

        self.assertEqual(self.recipients("company.member_removed"), {self.bystander.pk})
        row = Notification.objects.get(kind="company.member_removed")
        self.assertEqual(row.title, "You no longer have access to Team Notify Co")
        self.assertIn("Asha Owner removed you", row.body)
        self.assertEqual(row.target_url, "")

    def test_both_sides_of_an_ownership_transfer_are_told(self):
        target = CompanyMembership.objects.get(company=self.company, user=self.bystander)
        membership_service.transfer_ownership(
            actor=self.owner, actor_membership=self.owner_membership, target=target
        )
        deliver()

        self.assertEqual(
            self.recipients("company.ownership_transferred"), {self.owner.pk, self.bystander.pk}
        )
        row = Notification.objects.filter(kind="company.ownership_transferred").first()
        self.assertEqual(row.title, "Bina Bystander now owns Team Notify Co")
        self.assertIn("Asha Owner is now a company admin", row.body)
