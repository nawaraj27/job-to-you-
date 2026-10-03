"""
URL-scoped tenancy, proved through real HTTP (ADR-A18).

The scenario this exists for: a recruiter at two companies, two browser tabs,
one session. Under session-scoped tenancy the tab that loses the race writes
into the wrong company. These tests assert that the URL decides, every time.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.companies.models import CompanyMembership, CompanyState
from apps.companies.permissions import Role
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member, company_with_owner


class TenancyResolutionTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.first, _ = company_with_owner(self.user, slug="first-co", name="First Co")
        self.second, _ = company_with_owner(self.user, slug="second-co", name="Second Co")
        self.client.force_login(self.user)

    def test_each_url_resolves_its_own_company(self):
        first = self.client.get(reverse("employer:overview", args=["first-co"]))
        second = self.client.get(reverse("employer:overview", args=["second-co"]))

        self.assertEqual(first.context["company"], self.first)
        self.assertEqual(second.context["company"], self.second)

    def test_two_tabs_on_two_companies_do_not_interfere(self):
        # The whole reason tenancy is in the URL. Interleaved requests, one
        # session, and each still resolves its own tenant.
        first_a = self.client.get(reverse("employer:overview", args=["first-co"]))
        second_a = self.client.get(reverse("employer:overview", args=["second-co"]))
        first_b = self.client.get(reverse("employer:overview", args=["first-co"]))

        self.assertEqual(first_a.context["company"], self.first)
        self.assertEqual(second_a.context["company"], self.second)
        self.assertEqual(first_b.context["company"], self.first)

    def test_the_session_hint_never_overrides_the_url(self):
        self.client.get(reverse("employer:overview", args=["second-co"]))

        response = self.client.get(reverse("employer:overview", args=["first-co"]))

        self.assertEqual(response.context["company"], self.first)


class CrossTenantTests(TestCase):
    def setUp(self):
        self.insider = create_user()
        self.outsider = create_user()
        self.company, self.owner_membership = company_with_owner(self.insider, slug="acme-co")
        self.other, self.other_membership = company_with_owner(self.outsider, slug="other-co")

    def test_a_stranger_gets_404_not_403(self):
        # A 403 confirms the company exists to someone with no right to know.
        self.client.force_login(self.outsider)

        response = self.client.get(reverse("employer:overview", args=["acme-co"]))

        self.assertEqual(response.status_code, 404)

    def test_a_nonexistent_company_is_indistinguishable_from_a_forbidden_one(self):
        self.client.force_login(self.outsider)

        forbidden = self.client.get(reverse("employer:overview", args=["acme-co"]))
        missing = self.client.get(reverse("employer:overview", args=["no-such-co"]))

        self.assertEqual(forbidden.status_code, missing.status_code)

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        response = self.client.get(reverse("employer:overview", args=["acme-co"]))
        self.assertEqual(response.status_code, 302)

    def test_a_revoked_member_loses_access_in_the_same_request_cycle(self):
        self.client.force_login(self.insider)
        self.assertEqual(
            self.client.get(reverse("employer:overview", args=["acme-co"])).status_code, 200
        )

        CompanyMembership.objects.filter(pk=self.owner_membership.pk).update(
            deleted_at="2026-01-01T00:00:00Z"
        )

        self.assertEqual(
            self.client.get(reverse("employer:overview", args=["acme-co"])).status_code, 404
        )

    def test_a_suspended_company_locks_every_surface_at_once(self):
        self.client.force_login(self.insider)
        self.company.state = CompanyState.SUSPENDED
        self.company.save()

        self.assertEqual(
            self.client.get(reverse("employer:overview", args=["acme-co"])).status_code, 404
        )

    def test_a_member_cannot_act_on_another_company_s_membership(self):
        # Posting a foreign membership id must not reach the service layer.
        self.client.force_login(self.insider)

        response = self.client.post(
            reverse("employer:team", args=["acme-co"]),
            {"action": "remove", "membership_id": self.other_membership.pk},
        )

        self.assertEqual(response.status_code, 404)
        self.other_membership.refresh_from_db()
        self.assertTrue(self.other_membership.is_active)


class TeamViewTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner, slug="acme-co")
        self.recruiter = create_user()
        add_member(self.company, self.recruiter, Role.RECRUITER)

    def test_an_owner_sees_the_invite_form(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("employer:team", args=["acme-co"]))

        self.assertTrue(response.context["can_manage_team"])

    def test_a_recruiter_does_not(self):
        self.client.force_login(self.recruiter)

        response = self.client.get(reverse("employer:team", args=["acme-co"]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage_team"])

    def test_a_recruiter_posting_an_invite_is_refused(self):
        # Hiding the form is UX; refusing the POST is the control.
        self.client.force_login(self.recruiter)

        self.client.post(
            reverse("employer:team", args=["acme-co"]),
            {"action": "invite", "email": "x@example.com", "role": Role.RECRUITER},
        )

        self.assertEqual(self.company.invites.count(), 0)

    def test_an_owner_can_invite(self):
        self.client.force_login(self.owner)

        self.client.post(
            reverse("employer:team", args=["acme-co"]),
            {"action": "invite", "email": "new@example.com", "role": Role.RECRUITER},
        )

        self.assertEqual(self.company.invites.filter(email="new@example.com").count(), 1)


class SwitcherTests(TestCase):
    def test_a_user_with_no_company_is_sent_to_registration(self):
        self.client.force_login(create_user())

        response = self.client.get(reverse("employer:index"))

        self.assertRedirects(response, reverse("employer:create"))

    def test_a_user_with_one_company_skips_the_chooser(self):
        user = create_user()
        company_with_owner(user, slug="only-co")
        self.client.force_login(user)

        response = self.client.get(reverse("employer:index"))

        self.assertRedirects(response, reverse("employer:overview", args=["only-co"]))


class InviteAcceptanceViewTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner, slug="acme-co")
        _, self.token = membership_service.invite(
            actor=self.owner,
            actor_membership=self.owner_membership,
            company=self.company,
            email="invited@example.com",
            role=Role.RECRUITER,
        )

    def test_the_invited_address_can_accept(self):
        invitee = create_user(email="invited@example.com")
        self.client.force_login(invitee)

        response = self.client.post(reverse("employer:accept_invite", args=[self.token]))

        self.assertRedirects(response, reverse("employer:overview", args=["acme-co"]))

    def test_a_different_address_cannot(self):
        stranger = create_user(email="stranger@example.com")
        self.client.force_login(stranger)

        self.client.post(reverse("employer:accept_invite", args=[self.token]))

        self.assertFalse(
            CompanyMembership.objects.filter(company=self.company, user=stranger).exists()
        )

    def test_the_mismatch_is_shown_before_the_user_tries(self):
        stranger = create_user(email="stranger@example.com")
        self.client.force_login(stranger)

        response = self.client.get(reverse("employer:accept_invite", args=[self.token]))

        self.assertContains(response, "different address")


class LegacyPortalTests(TestCase):
    """The mock portal is gone (P8). One scheme, and it is company-scoped."""

    def test_the_legacy_flat_urls_no_longer_exist(self):
        # `apps/employers` and its session-backed sample data were deleted when
        # the real applicant pipeline landed. A flat `/employers/...` URL that
        # still answered would mean two employer portals, which is what SRS
        # §6.3 permitted only until this phase.
        for path in ("/employers/", "/employers/overview/", "/employers/applicants/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_every_employer_screen_is_company_scoped(self):
        user = create_user()
        company_with_owner(user, slug="acme-co")
        self.client.force_login(user)

        for name in ("overview", "job_list", "applicants", "team", "profile"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"employer:{name}", args=["acme-co"]))
                self.assertEqual(response.status_code, 200)

    def test_the_public_marketing_page_survived_the_deletion(self):
        # It was never mock data — it lived in `apps/employers` only because
        # that app happened to exist. It is a core page now.
        response = self.client.get(reverse("core:employer_solutions"))
        self.assertEqual(response.status_code, 200)


class InviteAddressDecisionTests(TestCase):
    """Decision D-03: an invitation is never bound to a different account.

    The account accepts when its main address or an already verified secondary
    address is the invited one. Anyone else is told to sign in as that address
    or to ask for the invitation again, and there is no "join with this account
    instead" button to press.
    """

    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner, slug="d03-co")
        _, self.token = membership_service.invite(
            actor=self.owner,
            actor_membership=self.owner_membership,
            company=self.company,
            email="work@example.com",
            role=Role.RECRUITER,
        )
        self.url = reverse("employer:accept_invite", args=[self.token])

    def _person(self, *, secondary: str = "", verified: bool = True):
        from django.utils import timezone

        from apps.accounts.models import EmailAddress

        person = create_user(email="personal@example.com")
        if secondary:
            EmailAddress.objects.create(
                user=person, email=secondary, verified_at=timezone.now() if verified else None
            )
        return person

    def test_a_verified_secondary_address_is_offered_the_button_and_accepts(self):
        person = self._person(secondary="work@example.com")
        self.client.force_login(person)

        page = self.client.get(self.url)
        self.assertContains(page, "Accept invitation")
        self.assertNotContains(page, "different address")

        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("employer:overview", args=["d03-co"]))
        self.assertTrue(
            CompanyMembership.objects.filter(company=self.company, user=person).exists()
        )

    def test_an_unverified_secondary_address_does_not_count(self):
        person = self._person(secondary="work@example.com", verified=False)
        self.client.force_login(person)

        page = self.client.get(self.url)
        self.assertNotContains(page, "Accept invitation")
        self.client.post(self.url)

        self.assertFalse(
            CompanyMembership.objects.filter(company=self.company, user=person).exists()
        )

    def test_a_mismatch_offers_sign_out_or_a_resend_and_no_way_to_bind(self):
        self.client.force_login(self._person())

        page = self.client.get(self.url)

        self.assertContains(page, "work@example.com")
        self.assertContains(page, "send the invitation again")
        self.assertContains(page, reverse("accounts:logout"))
        self.assertNotContains(page, "Accept invitation")
        for wording in ("Bind", "bind the invitation", "this account instead", "current account"):
            self.assertNotContains(page, wording)

    def test_a_refused_post_leaves_the_invitation_open_for_the_right_person(self):
        from apps.companies.models import CompanyInvite

        self.client.force_login(self._person())
        response = self.client.post(self.url, follow=True)

        self.assertContains(response, "different email address")
        invite = CompanyInvite.objects.get(company=self.company)
        self.assertTrue(invite.is_open)
        self.assertIsNone(invite.accepted_by)
