"""
Billing defects reproduced on the running product (section loop, 2026-09-15).

  * an unpaid subscription never ended: the nightly renewal rolled a `past_due`
    period forward and set it `active`, so the grace clock — measured from the
    period end — kept moving (SRS §5.14: withdrawal after fourteen days);
  * paying this month's invoice cleared `past_due` while last month's was still
    unpaid;
  * two invoices raised with one coupon counted one redemption;
  * a payment in another currency settled an invoice ("NPR 12.00" paid a
    "USD 12.00" invoice), and the gateway signature did not cover the currency;
  * any employer billing post other than "cancel" resumed a cancelled plan;
  * the usage panel showed candidates "Open postings" and employers "CV analyses".
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.billing import tasks
from apps.billing.models import Coupon, Invoice, InvoiceState, SubscriptionState
from apps.billing.providers import base as providers
from apps.billing.providers.fake import provider as fake_gateway
from apps.billing.providers.fake import sign
from apps.billing.services import invoices, payments, subscriptions
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner


class BillingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        factories.seed()

    def setUp(self):
        fake_gateway.reset()
        self.profile = create_profile()


class UnpaidLifecycleTests(BillingFixture):
    def run_nights(self, subscription, start: dt.date, nights: int) -> dt.date | None:
        day = start
        for _ in range(nights):
            day += dt.timedelta(days=1)
            iso = day.isoformat()
            tasks.renew_due(iso)
            tasks.chase_overdue(iso)
            tasks.end_cancelled(iso)
            tasks.end_unpaid(iso)
            subscription.refresh_from_db()
            if not subscription.is_live:
                return day
        return None

    def test_a_subscription_that_never_pays_ends_after_the_grace_period(self):
        start = timezone.localdate() - dt.timedelta(days=120)
        subscription = subscriptions.subscribe(
            owner=self.profile, plan=factories.pro_plan(), starts_on=start
        )
        invoice = invoices.issue(subscription=subscription)
        Invoice.objects.filter(pk=invoice.pk).update(
            issued_on=start, due_on=start + dt.timedelta(days=invoices.DUE_DAYS)
        )

        ended_on = self.run_nights(subscription, start, 90)

        period_end = subscriptions.next_period_end(start, subscription.plan.interval)
        self.assertIsNotNone(ended_on, "an unpaid subscription kept its plan for 90 days")
        self.assertLessEqual(ended_on, period_end + dt.timedelta(days=subscriptions.GRACE_DAYS + 1))
        self.assertEqual(subscription.state, SubscriptionState.EXPIRED)
        # And it was not billed for periods it never paid for.
        self.assertEqual(Invoice.objects.filter(subscription=subscription).count(), 1)

    def test_a_past_due_subscription_is_not_renewed(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.mark_past_due(subscription=subscription, invoice_number="INV-1")
        from apps.billing.models import Subscription

        Subscription.objects.filter(pk=subscription.pk).update(
            current_period_end=timezone.localdate() - dt.timedelta(days=1)
        )

        self.assertEqual(tasks.renew_due(), 0)
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, SubscriptionState.PAST_DUE)

    def test_renewing_keeps_a_past_due_state(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.mark_past_due(subscription=subscription, invoice_number="INV-1")
        subscriptions.renew(subscription=subscription)
        self.assertEqual(subscription.state, SubscriptionState.PAST_DUE)

    def test_paying_the_newer_invoice_leaves_an_older_overdue_one_past_due(self):
        start = timezone.localdate() - dt.timedelta(days=40)
        subscription = subscriptions.subscribe(
            owner=self.profile, plan=factories.pro_plan(), starts_on=start
        )
        older = invoices.issue(subscription=subscription)
        Invoice.objects.filter(pk=older.pk).update(due_on=start + dt.timedelta(days=14))
        subscriptions.renew(subscription=subscription)
        newer = invoices.issue(subscription=subscription)
        subscriptions.mark_past_due(subscription=subscription, invoice_number=older.number)

        payments.record_manual(invoice=newer, actor=self.profile.user, reference="BANK-NEW")
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, SubscriptionState.PAST_DUE)

        payments.record_manual(invoice=older, actor=self.profile.user, reference="BANK-OLD")
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, SubscriptionState.ACTIVE)


class CouponCountTests(BillingFixture):
    def test_two_invoices_with_one_coupon_count_twice(self):
        coupon = Coupon.objects.create(code="TWICE", percent_off=10, max_redemptions=5)
        first = factories.put_on_pro(self.profile)
        second = factories.put_on_pro(create_profile())

        invoices.issue(subscription=first, coupon=coupon)
        invoices.issue(subscription=second, coupon=coupon)

        coupon.refresh_from_db()
        self.assertEqual(coupon.redemptions, 2)

    def test_a_coupon_that_took_nothing_off_is_not_counted(self):
        other_plan = factories.make_plan(code="elsewhere", price=5)
        coupon = Coupon.objects.create(code="ELSEWHERE", percent_off=50, plan=other_plan)

        invoice = invoices.issue(subscription=factories.put_on_pro(self.profile), coupon=coupon)

        coupon.refresh_from_db()
        self.assertEqual(invoice.discount, 0)
        self.assertEqual(coupon.redemptions, 0)


class CurrencyTests(BillingFixture):
    def setUp(self):
        super().setUp()
        self.invoice = invoices.issue(subscription=factories.put_on_pro(self.profile))

    def other_currency(self) -> str:
        """Any currency that is not the invoice's (the catalogue is in NPR)."""
        return "USD" if self.invoice.currency != "USD" else "NPR"

    def test_a_payment_in_another_currency_does_not_settle_the_invoice(self):
        other = self.other_currency()
        payload = fake_gateway.callback_for(invoice=self.invoice)
        payload["currency"] = other
        payload["signature"] = sign(payload)

        payment = payments.settle_callback(provider_key="fake", payload=payload)

        self.invoice.refresh_from_db()
        self.assertEqual(payment.currency, other)
        self.assertEqual(self.invoice.state, InvoiceState.OPEN)

    def test_the_currency_is_covered_by_the_signature(self):
        payload = fake_gateway.callback_for(invoice=self.invoice)
        payload["currency"] = self.other_currency()

        with self.assertRaises(providers.PaymentUnverified):
            payments.settle_callback(provider_key="fake", payload=payload)

    def test_the_same_currency_still_settles(self):
        payments.settle_callback(
            provider_key="fake", payload=fake_gateway.callback_for(invoice=self.invoice)
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, InvoiceState.PAID)


class EmployerBillingActionTests(BillingFixture):
    def setUp(self):
        super().setUp()
        self.owner = self.profile.user
        self.company, _ = company_with_owner(self.owner)
        self.subscription = subscriptions.subscribe(
            owner=self.company, plan=factories.employer_plan()
        )
        subscriptions.cancel(subscription=self.subscription, actor=self.owner)
        self.url = reverse("employer:billing", args=[self.company.slug])
        self.client.force_login(self.owner)

    def test_an_unknown_action_does_not_resume_a_cancellation(self):
        response = self.client.post(self.url, {"action": "refresh"}, follow=True)

        self.subscription.refresh_from_db()
        self.assertIsNotNone(self.subscription.cancel_at)
        self.assertContains(response, "That is not something this page can do.")

    def test_resume_is_still_explicit(self):
        self.client.post(self.url, {"action": "resume"})
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.cancel_at)


class UsagePanelMarkupTests(BillingFixture):
    """axe serious `definition-list` / `dlitem` on both plan screens."""

    def assert_terms_sit_one_div_under_the_list(self, html: str) -> None:
        compact = " ".join(html.split())
        self.assertIn('<div class="card card-body"> <dt', compact)
        self.assertNotIn('<div class="card-body"> <dt', compact)

    def test_the_candidate_plan_panel_is_a_valid_definition_list(self):
        self.client.force_login(self.profile.user)
        page = self.client.get(reverse("dashboard:plan"))
        self.assert_terms_sit_one_div_under_the_list(page.content.decode())

    def test_the_company_billing_panel_is_a_valid_definition_list(self):
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        self.client.force_login(owner)
        page = self.client.get(reverse("employer:billing", args=[company.slug]))
        self.assert_terms_sit_one_div_under_the_list(page.content.decode())


class UsagePanelAudienceTests(BillingFixture):
    def test_a_candidate_is_not_shown_company_limits(self):
        self.client.force_login(self.profile.user)
        page = self.client.get(reverse("dashboard:plan"))

        self.assertContains(page, "CV analyses")
        self.assertNotContains(page, "Open postings")

    def test_an_employer_is_not_shown_candidate_limits(self):
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        self.client.force_login(owner)

        page = self.client.get(reverse("employer:billing", args=[company.slug]))

        self.assertContains(page, "Open postings")
        self.assertNotContains(page, "CV analyses")
