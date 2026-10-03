"""
SRS §2.6: "Ownership transfer is an explicit two-party flow: the current owner
nominates, the nominee accepts, both are notified, and the transfer is audited"
(Loop Closure EMP-05).

The team screen moved ownership on one click by the owner; the person receiving
billing and legal control of the company was never asked.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.companies.models import CompanyMembership, OwnershipTransfer
from apps.companies.permissions import Role
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member, company_with_owner
from apps.companies.tests.test_team_notifications import deliver
from apps.notifications.models import Notification


class NominationFixture(TestCase):
    def setUp(self):
        self.owner = create_user(full_name="Asha Owner")
        self.company, self.owner_membership = company_with_owner(
            self.owner, slug="nominate-co", name="Nominate Co"
        )
        self.admin = create_user(full_name="Bina Admin")
        self.admin_membership = add_member(self.company, self.admin, Role.ADMIN)
        self.recruiter = create_user(full_name="Chandra Recruiter")
        self.recruiter_membership = add_member(self.company, self.recruiter, Role.RECRUITER)
        self.team_url = reverse("employer:team", args=["nominate-co"])

    def roles(self):
        return dict(
            CompanyMembership.objects.filter(company=self.company).values_list("user_id", "role")
        )

    def nominate(self, target=None):
        return membership_service.nominate_owner(
            actor=self.owner,
            actor_membership=self.owner_membership,
            target=target or self.admin_membership,
        )


class NominationServiceTests(NominationFixture):
    def test_nominating_changes_nothing_until_the_nominee_accepts(self):
        self.nominate()
        self.assertEqual(self.roles()[self.owner.pk], Role.OWNER)
        self.assertEqual(self.roles()[self.admin.pk], Role.ADMIN)

        membership_service.respond_to_nomination(
            actor=self.admin, actor_membership=self.admin_membership, accept=True
        )

        self.assertEqual(self.roles()[self.admin.pk], Role.OWNER)
        self.assertEqual(self.roles()[self.owner.pk], Role.ADMIN)
        self.assertEqual(OwnershipTransfer.objects.get().state, OwnershipTransfer.State.ACCEPTED)

    def test_declining_leaves_ownership_alone_and_is_audited(self):
        self.nominate()
        membership_service.respond_to_nomination(
            actor=self.admin, actor_membership=self.admin_membership, accept=False
        )

        self.assertEqual(self.roles()[self.owner.pk], Role.OWNER)
        self.assertTrue(AuditLog.objects.filter(verb="company.ownership_declined").exists())

    def test_only_the_nominee_can_answer(self):
        self.nominate()
        for actor, membership in (
            (self.recruiter, self.recruiter_membership),
            (self.owner, self.owner_membership),
        ):
            with self.subTest(actor=actor.full_name), self.assertRaises(NotFound):
                membership_service.respond_to_nomination(
                    actor=actor, actor_membership=membership, accept=True
                )
        self.assertEqual(self.roles()[self.owner.pk], Role.OWNER)

    def test_only_an_owner_nominates_and_only_one_nomination_waits(self):
        with self.assertRaises(InvalidState):
            membership_service.nominate_owner(
                actor=self.admin,
                actor_membership=self.admin_membership,
                target=self.recruiter_membership,
            )
        self.nominate()
        with self.assertRaises(Conflict):
            self.nominate(self.recruiter_membership)

    def test_an_owner_can_withdraw_and_the_nominee_is_told(self):
        self.nominate()
        membership_service.cancel_nomination(
            actor=self.owner, actor_membership=self.owner_membership
        )
        deliver()

        with self.assertRaises(NotFound):
            membership_service.respond_to_nomination(
                actor=self.admin, actor_membership=self.admin_membership, accept=True
            )
        row = Notification.objects.get(kind="company.ownership_nomination_closed")
        self.assertEqual(row.recipient, self.admin)

    def test_the_nominee_is_told_and_both_are_told_of_the_transfer(self):
        self.nominate()
        deliver()
        nominated = Notification.objects.get(kind="company.ownership_nominated")
        self.assertEqual(nominated.recipient, self.admin)
        self.assertEqual(nominated.title, "Asha Owner nominated you to own Nominate Co")

        membership_service.respond_to_nomination(
            actor=self.admin, actor_membership=self.admin_membership, accept=True
        )
        deliver()
        self.assertEqual(
            set(
                Notification.objects.filter(kind="company.ownership_transferred").values_list(
                    "recipient_id", flat=True
                )
            ),
            {self.owner.pk, self.admin.pk},
        )

    def test_a_nomination_lapses_if_the_nominator_stopped_being_owner(self):
        self.nominate()
        second_owner = add_member(self.company, create_user(), Role.OWNER)
        membership_service.change_role(
            actor=second_owner.user,
            actor_membership=second_owner,
            target=self.owner_membership,
            role=Role.ADMIN,
        )

        with self.assertRaisesMessage(InvalidState, "lapsed"):
            membership_service.respond_to_nomination(
                actor=self.admin, actor_membership=self.admin_membership, accept=True
            )
        self.assertEqual(self.roles()[self.admin.pk], Role.ADMIN)

    def test_removing_the_nominee_cancels_the_nomination(self):
        self.nominate()
        membership_service.remove_member(
            actor=self.owner, actor_membership=self.owner_membership, target=self.admin_membership
        )
        self.assertIsNone(membership_service.pending_nomination(self.company))
        self.nominate(self.recruiter_membership)  # no longer blocked


class NominationScreenTests(NominationFixture):
    def test_the_owner_screen_asks_rather_than_transfers(self):
        self.client.force_login(self.owner)
        page = self.client.get(self.team_url)
        self.assertContains(page, 'value="nominate"')
        self.assertNotContains(page, 'value="transfer"')

        self.client.post(
            self.team_url, {"action": "nominate", "membership_id": self.admin_membership.pk}
        )
        self.assertEqual(self.roles()[self.owner.pk], Role.OWNER)
        page = self.client.get(self.team_url)
        self.assertContains(page, "Waiting for Bina Admin to accept ownership")
        self.assertContains(page, 'value="cancel_nomination"')

    def test_the_nominee_accepts_from_the_team_screen(self):
        self.nominate()
        self.client.force_login(self.admin)
        page = self.client.get(self.team_url)
        self.assertContains(page, "Asha Owner has asked you to own Nominate Co")

        response = self.client.post(self.team_url, {"action": "accept_ownership"}, follow=True)
        self.assertContains(response, "You now own Nominate Co.")
        self.assertEqual(self.roles()[self.admin.pk], Role.OWNER)

    def test_a_stale_direct_transfer_post_is_not_an_action_any_more(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.team_url, {"action": "transfer", "membership_id": self.admin_membership.pk}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.roles()[self.owner.pk], Role.OWNER)

    def test_a_recruiter_sees_the_waiting_nomination_but_cannot_answer_it(self):
        self.nominate()
        self.client.force_login(self.recruiter)
        page = self.client.get(self.team_url)
        self.assertContains(page, "Waiting for Bina Admin")
        self.assertNotContains(page, 'value="accept_ownership"')

        response = self.client.post(self.team_url, {"action": "accept_ownership"}, follow=True)
        self.assertContains(response, "no nomination waiting for you")
