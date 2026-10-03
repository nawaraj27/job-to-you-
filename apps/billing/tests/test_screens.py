"""
The billing screens (SRS §5.14, §2.4).

What is worth asserting through a screen rather than only a service:

  * **the read side is scoped** — one company's invoices never appear on
    another's page, asserted from the failing side;
  * **the write side is a grant, not a role** — a company admin can do nearly
    everything else and deliberately cannot move money;
  * **the page reflects the plan** rather than deciding it, which is what makes
    "enforced in a service, never in a template" checkable from outside.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.billing.models import SubscriptionState
from apps.billing.services import entitlements, invoices, subscriptions
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner


class ScreenFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        factories.seed()

    def setUp(self):
        self.profile = create_profile()
        self.user = self.profile.user
        self.client.force_login(self.user)
        self.url = reverse("dashboard:plan")


class CandidatePlanScreenTests(ScreenFixture):
    def test_the_page_renders_for_somebody_on_the_free_plan(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Free")

    def test_the_usage_panel_shows_what_is_left(self):
        response = self.client.get(self.url)
        rows = {row["key"]: row for row in response.context["usage"]}
        self.assertEqual(rows["resume_analyses"]["remaining"], 1)

    def test_the_other_plan_is_offered(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Pro")

    def test_moving_onto_a_plan_creates_the_checkout_and_the_invoice(self):
        # ADR-A59: the invoice now, the plan when it is paid.
        from apps.billing.services import subscriptions

        response = self.client.post(reverse("dashboard:plan_start"), {"plan": "pro"})

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(entitlements.live_subscription(self.profile))
        waiting = subscriptions.pending_checkout(self.profile)
        self.assertEqual(waiting.plan.code, "pro")
        self.assertEqual(waiting.invoices.count(), 1)
        page = self.client.get(self.url)
        self.assertContains(page, "Pro starts when this invoice is paid")
        self.assertContains(page, "NPR 1,700")

    def test_an_unknown_plan_code_does_not_500(self):
        response = self.client.post(reverse("dashboard:plan_start"), {"plan": "diamond"})
        self.assertEqual(response.status_code, 302)

    def test_cancelling_from_the_page_schedules_the_end(self):
        subscription = factories.put_on_pro(self.profile)
        self.client.post(self.url, {"action": "cancel"})

        subscription.refresh_from_db()
        self.assertIsNotNone(subscription.cancel_at)
        self.assertTrue(subscription.is_live)

    def test_resuming_clears_it(self):
        subscription = factories.put_on_pro(self.profile)
        self.client.post(self.url, {"action": "cancel"})
        self.client.post(self.url, {"action": "resume"})

        subscription.refresh_from_db()
        self.assertIsNone(subscription.cancel_at)

    def test_an_unknown_action_changes_nothing(self):
        subscription = factories.put_on_pro(self.profile)
        self.client.post(self.url, {"action": "delete-everything"})

        subscription.refresh_from_db()
        self.assertEqual(subscription.state, SubscriptionState.ACTIVE)

    def test_another_candidates_invoice_is_not_listed(self):
        stranger = create_profile()
        other = factories.put_on_pro(stranger)
        invoice = invoices.issue(subscription=other)

        response = self.client.get(self.url)
        self.assertNotContains(response, invoice.number)

    def test_signing_out_closes_the_screen(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class EmployerBillingScreenTests(ScreenFixture):
    def setUp(self):
        super().setUp()
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.client.force_login(self.owner)
        self.url = reverse("employer:billing", args=[self.company.slug])

    def test_the_owner_sees_the_plan(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Employer")

    def test_a_recruiter_has_no_billing_page(self):
        # SRS §2.3: Billing is "None" for every role below Company Admin.
        recruiter = create_profile().user
        add_member(self.company, recruiter, role=Role.RECRUITER)
        self.client.force_login(recruiter)

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_company_admin_may_read_but_not_change(self):
        admin = create_profile().user
        add_member(self.company, admin, role=Role.ADMIN)
        self.client.force_login(admin)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage"])

    def test_a_company_admin_cannot_change_the_plan(self):
        # `billing.manage` is deliberately absent from the admin preset: an
        # admin can do nearly everything else and cannot move money.
        subscriptions.subscribe(owner=self.company, plan=factories.employer_plan())
        admin = create_profile().user
        add_member(self.company, admin, role=Role.ADMIN)
        self.client.force_login(admin)

        response = self.client.post(self.url, {"action": "cancel"})
        self.assertEqual(response.status_code, 403)

    def test_a_stranger_cannot_see_the_page(self):
        self.client.force_login(create_profile().user)
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (302, 403, 404))

    def test_another_companys_invoice_is_not_listed(self):
        other_owner = create_profile().user
        other_company, _ = company_with_owner(other_owner)
        subscription = subscriptions.subscribe(
            owner=other_company,
            plan=factories.make_plan(code="other", audience="company", price=10, currency="USD"),
        )
        invoice = invoices.issue(subscription=subscription)

        response = self.client.get(self.url)
        self.assertNotContains(response, invoice.number)


class PricingPageTests(ScreenFixture):
    def test_the_public_page_describes_the_real_plans(self):
        self.client.logout()
        response = self.client.get(reverse("core:pricing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pro")

    def test_the_page_no_longer_advertises_templates_that_do_not_exist(self):
        # The hand-written list claimed twelve templates where six exist.
        # Generated bullets cannot make that claim again.
        self.client.logout()
        response = self.client.get(reverse("core:pricing"))
        self.assertNotContains(response, "12 CV templates")

    def test_the_advertised_analysis_limit_is_the_enforced_one(self):
        from apps.billing.selectors import plan_cards

        free = next(card for card in plan_cards() if card["code"] == "free")
        self.assertIn("1 CV analysis per month", free["features"])
