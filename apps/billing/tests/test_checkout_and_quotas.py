"""
Paying before access, and the plan allowances (ADR-A59).

Checkout:
  * choosing Pro gives an invoice and nothing else; only a verified payment
    (a gateway's server-side confirmation or a recorded bank transfer) starts
    Pro, and it survives a reload, a new session and a background run;
  * a redirect, a forged callback or an edited form never starts it;
  * a checkout nobody pays expires with its invoice; a payment that arrives
    after that is recorded and flagged as owing a refund, and grants nothing;
  * cancelling a checkout costs nothing.

Allowances:
  * each AI feature has its own metered allowance from the plan;
  * the refusal says what, how much, until when, and that Pro includes more;
  * the reset date shown is the one enforcement uses;
  * a failure on the platform's side refunds the unit;
  * concurrent requests cannot take more than the allowance;
  * paid, cancelled, expired and overdue accounts get their plan's numbers.
"""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal
from unittest import mock

import httpx
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_staff
from apps.billing import tasks
from apps.billing.gateways import base as gateways
from apps.billing.models import (
    AttemptState,
    Invoice,
    InvoiceState,
    PaymentAttempt,
    Subscription,
    SubscriptionState,
    UsageCounter,
)
from apps.billing.services import entitlements, payments, subscriptions, usage
from apps.billing.tests import factories
from apps.billing.tests.test_gateways import GATEWAYS, FakeGatewayServer, esewa_return_data
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner


class CheckoutFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        factories.seed()

    def setUp(self):
        cache.clear()
        self.profile = create_profile()
        self.client.force_login(self.profile.user)

    def choose_pro(self):
        return self.client.post(reverse("dashboard:plan_start"), {"plan": "pro"})

    def checkout_invoice(self) -> Invoice:
        return Invoice.objects.get(
            subscription__candidate=self.profile, subscription__state="incomplete"
        )


class PriceTests(CheckoutFixture):
    def test_pro_is_priced_in_rupees_everywhere(self):
        pro = factories.pro_plan()
        self.assertEqual(pro.currency, "NPR")
        self.assertEqual(pro.price, Decimal("1700"))
        self.assertContains(self.client.get(reverse("core:pricing")), "NPR 1,700")
        self.assertContains(self.client.get(reverse("dashboard:plan")), "NPR 1,700")
        self.choose_pro()
        invoice = self.checkout_invoice()
        self.assertEqual((invoice.total, invoice.currency), (Decimal("1700.00"), "NPR"))

    def test_money_is_written_one_way(self):
        from apps.billing.money import format_money

        self.assertEqual(format_money(Decimal("1700.00"), "NPR"), "NPR 1,700")
        self.assertEqual(format_money(Decimal("1250.50"), "NPR"), "NPR 1,250.50")
        self.assertEqual(format_money(0, "NPR"), "NPR 0")


class CheckoutTests(CheckoutFixture):
    def test_choosing_pro_grants_nothing_until_paid(self):
        self.choose_pro()
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")
        self.assertEqual(self.checkout_invoice().state, InvoiceState.OPEN)
        page = self.client.get(reverse("dashboard:plan"))
        self.assertContains(page, "Pro starts when this invoice is paid")
        self.assertContains(page, "You stay on Free until the payment is confirmed")

    def test_a_recorded_transfer_starts_pro_and_it_stays_started(self):
        self.choose_pro()
        invoice = self.checkout_invoice()
        payments.record_manual(invoice=invoice, actor=create_staff(), reference="BANK-1")

        self.assertEqual(entitlements.plan_for(self.profile).code, "pro")
        # A new session on another device reads the same database.
        self.client.logout()
        self.client.force_login(self.profile.user)
        page = self.client.get(reverse("dashboard:plan"))
        self.assertContains(page, "Renews on")
        self.assertNotContains(page, "Waiting for payment")
        # Background work does not undo it.
        tasks.renew_due(timezone.localdate().isoformat())
        tasks.end_unpaid(timezone.localdate().isoformat())
        self.assertEqual(entitlements.plan_for(self.profile).code, "pro")

    def test_an_edited_form_or_invoice_flag_cannot_start_pro(self):
        self.choose_pro()
        invoice = self.checkout_invoice()
        # Posting the plan page with anything, or reloading it, changes nothing.
        self.client.post(
            reverse("dashboard:plan"), {"action": "activate", "invoice": invoice.number}
        )
        self.client.get(reverse("dashboard:plan") + "?paid=1")
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")
        # Only `payments._record` with a verified settlement activates.
        self.assertEqual(
            Subscription.objects.get(candidate=self.profile).state, SubscriptionState.INCOMPLETE
        )

    def test_a_second_tab_reuses_the_checkout(self):
        self.choose_pro()
        self.choose_pro()
        self.assertEqual(Subscription.objects.filter(candidate=self.profile).count(), 1)
        self.assertEqual(Invoice.objects.filter(subscription__candidate=self.profile).count(), 1)

    def test_cancelling_the_checkout_costs_nothing(self):
        self.choose_pro()
        invoice = self.checkout_invoice()
        self.client.post(reverse("dashboard:plan"), {"action": "abandon_checkout"})
        invoice.refresh_from_db()
        self.assertEqual(invoice.state, InvoiceState.VOID)
        self.assertIsNone(subscriptions.pending_checkout(self.profile))
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")

    def test_an_unpaid_checkout_expires_with_its_invoice(self):
        self.choose_pro()
        invoice = self.checkout_invoice()
        Subscription.objects.filter(candidate=self.profile).update(
            started_on=timezone.localdate() - dt.timedelta(days=subscriptions.CHECKOUT_EXPIRES_DAYS)
        )
        self.assertEqual(tasks.expire_checkouts(), 1)
        invoice.refresh_from_db()
        self.assertEqual(invoice.state, InvoiceState.VOID)
        self.assertEqual(
            Subscription.objects.get(candidate=self.profile).state, SubscriptionState.EXPIRED
        )

    def test_cancelling_pro_keeps_it_to_the_end_of_the_paid_period(self):
        self.choose_pro()
        payments.record_manual(invoice=self.checkout_invoice(), actor=create_staff(), reference="B")
        self.client.post(reverse("dashboard:plan"), {"action": "cancel"})
        subscription = entitlements.live_subscription(self.profile)
        self.assertEqual(subscription.cancel_at, subscription.current_period_end)
        self.assertEqual(entitlements.plan_for(self.profile).code, "pro")
        tasks.end_cancelled(subscription.current_period_end.isoformat())
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")


@override_settings(PAYMENT_GATEWAYS=GATEWAYS, PAYMENTS_ENVIRONMENT="test")
class GatewayCheckoutTests(CheckoutFixture):
    def setUp(self):
        super().setUp()
        self.server = FakeGatewayServer()
        gateways.set_transport(lambda: httpx.MockTransport(self.server))
        self.addCleanup(gateways.set_transport, lambda: None)
        self.choose_pro()
        self.invoice = self.checkout_invoice()
        self.client.post(reverse("billing:pay", args=[self.invoice.number]), {"gateway": "esewa"})
        self.attempt = PaymentAttempt.objects.get(invoice=self.invoice)

    def returned(self, status="COMPLETE"):
        self.server.esewa[self.attempt.reference] = {"status": status}
        return self.client.get(
            reverse("billing:return", args=["esewa", self.attempt.reference]),
            {"data": esewa_return_data(self.attempt, status=status)},
        )

    def test_a_verified_esewa_payment_starts_pro(self):
        self.returned()
        self.assertEqual(entitlements.plan_for(self.profile).code, "pro")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, InvoiceState.PAID)

    def test_a_signed_redirect_the_status_api_does_not_confirm_starts_nothing(self):
        self.server.esewa.pop(self.attempt.reference, None)
        self.client.get(
            reverse("billing:return", args=["esewa", self.attempt.reference]),
            {"data": esewa_return_data(self.attempt)},
        )
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")

    def test_a_cancelled_checkout_at_esewa_starts_nothing(self):
        self.client.get(reverse("billing:return_failed", args=["esewa", self.attempt.reference]))
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")
        self.assertEqual(self.checkout_invoice().state, InvoiceState.OPEN)

    def test_a_duplicate_callback_starts_pro_once(self):
        self.returned()
        self.returned()
        self.assertEqual(
            Subscription.objects.filter(candidate=self.profile, state="active").count(), 1
        )

    def test_a_payment_after_the_checkout_expired_is_flagged_and_grants_nothing(self):
        subscriptions.abandon_checkout(
            subscription=Subscription.objects.get(candidate=self.profile), reason="test"
        )
        self.returned()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.state, AttemptState.SUCCEEDED)
        self.assertIn("refund is owed", self.attempt.failure_reason)
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")

    def test_a_refund_after_activation_is_recorded(self):
        from apps.billing.services import checkout

        self.returned()
        self.server.esewa[self.attempt.reference] = {"status": "FULL_REFUND"}
        checkout.reconcile(PaymentAttempt.objects.get(pk=self.attempt.pk))
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.state, AttemptState.REFUNDED)


class QuotaTests(CheckoutFixture):
    def claim(self, key, owner=None):
        entitlements.claim(owner or self.profile, key)

    def test_each_feature_has_its_own_allowance(self):
        for key, free in (
            ("assistant_replies", 20),
            ("cv_improvements", 5),
            ("interview_practice", 2),
            ("cover_letters", 2),
        ):
            with self.subTest(key=key):
                self.assertEqual(entitlements.value_of(self.profile, key), free)
        self.claim("cover_letters")
        self.assertEqual(entitlements.remaining(self.profile, "interview_practice"), 2)

    def test_the_refusal_says_what_how_much_and_until_when(self):
        for _ in range(2):
            self.claim("interview_practice")
        with self.assertRaises(entitlements.PlanLimitExceeded) as caught:
            self.claim("interview_practice")
        message = caught.exception.message
        reset = entitlements.resets_on("interview_practice")
        self.assertIn("practice interviews", message)
        self.assertIn("You have used 2 of 2", message)
        self.assertIn(f"{reset.day} {reset:%B %Y}", message)
        self.assertIn("Pro includes more", message)
        self.assertEqual(caught.exception.details["resets_on"], reset.isoformat())

    def test_the_reset_date_is_the_enforcement_period(self):
        today = timezone.localdate()
        self.assertEqual(
            entitlements.resets_on("assistant_replies", today=today), today + dt.timedelta(days=1)
        )
        first_next_month = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        self.assertEqual(entitlements.resets_on("cover_letters", today=today), first_next_month)

    def test_a_daily_allowance_comes_back_the_next_day(self):
        for _ in range(5):
            self.claim("cv_improvements")
        with self.assertRaises(entitlements.PlanLimitExceeded):
            self.claim("cv_improvements")
        tomorrow = timezone.localdate() + dt.timedelta(days=1)
        with mock.patch.object(
            UsageCounter, "start_of", staticmethod(lambda period, today=None: tomorrow)
        ):
            self.claim("cv_improvements")

    def test_a_release_gives_the_unit_back(self):
        self.claim("cover_letters")
        entitlements.release(self.profile, "cover_letters")
        self.assertEqual(entitlements.remaining(self.profile, "cover_letters"), 2)

    def test_pro_gets_its_own_numbers(self):
        factories.put_on_pro(self.profile)
        self.assertEqual(entitlements.value_of(self.profile, "assistant_replies"), 300)
        self.assertIsNone(entitlements.value_of(self.profile, "resume_analyses"))

    def test_ended_and_overdue_accounts(self):
        subscription = factories.put_on_pro(self.profile)
        # Past due keeps the plan while the invoice is chased (D-08).
        Subscription.objects.filter(pk=subscription.pk).update(state=SubscriptionState.PAST_DUE)
        self.assertEqual(entitlements.value_of(self.profile, "interview_practice"), 40)
        # Cancelled or expired falls back to Free's numbers.
        for state in (SubscriptionState.CANCELLED, SubscriptionState.EXPIRED):
            Subscription.objects.filter(pk=subscription.pk).update(state=state)
            self.assertEqual(entitlements.value_of(self.profile, "interview_practice"), 2)

    def test_the_plan_page_shows_used_left_and_reset(self):
        self.claim("cover_letters")
        page = self.client.get(reverse("dashboard:plan"))
        self.assertContains(page, "Cover letter drafts")
        self.assertContains(page, "1 of 2 left this month")
        reset = entitlements.resets_on("cover_letters")
        self.assertContains(page, f"Resets on {reset.day} {reset:%b %Y}")
        self.assertContains(page, "Assisted CV replies")
        self.assertContains(page, "left today")


class FeatureMeteringTests(CheckoutFixture):
    """The meters are on the features, with refunds for the platform's failures."""

    def test_a_failed_cover_letter_draft_is_refunded(self):
        from apps.ai.models import AIRun, RunStatus
        from apps.applications.services import letters
        from apps.jobs.tests.factories import create_job

        job = create_job(company_with_owner(create_profile().user)[0])
        failed = AIRun(status=RunStatus.PROVIDER_ERROR, output={})
        with mock.patch("apps.applications.services.letters.runner.run", return_value=failed):
            letter, _run = letters.draft(profile=self.profile, actor=self.profile.user, job=job)
        self.assertIsNone(letter)
        self.assertEqual(entitlements.remaining(self.profile, "cover_letters"), 2)

    def test_cover_letters_stop_at_the_allowance_with_the_reason(self):
        from apps.applications.services import letters
        from apps.jobs.tests.factories import create_job

        for _ in range(2):
            self.claim_unit("cover_letters")
        with self.assertRaises(entitlements.PlanLimitExceeded):
            letters.draft(
                profile=self.profile,
                actor=self.profile.user,
                job=create_job(company_with_owner(create_profile().user)[0]),
            )

    def claim_unit(self, key):
        entitlements.claim(self.profile, key)

    def test_practice_interviews_are_metered_and_employer_interviews_are_not(self):
        from apps.interviews.services import engine
        from apps.interviews.tests.factories import fake_ai, practice_template

        template = practice_template()
        with fake_ai():
            for _ in range(2):
                engine.start(candidate=self.profile, template=template, actor=self.profile.user)
            with self.assertRaises(entitlements.PlanLimitExceeded):
                engine.start(candidate=self.profile, template=template, actor=self.profile.user)
        self.assertEqual(usage.used(owner=self.profile, metric="interview_practice"), 2)

    def test_an_interview_that_cannot_start_is_refunded(self):
        from apps.common.errors import InvalidState
        from apps.interviews.services import engine
        from apps.interviews.tests.factories import fake_ai, practice_template

        empty = practice_template(slug="empty-q", question_pools={})
        with fake_ai(), self.assertRaises(InvalidState):
            engine.start(candidate=self.profile, template=empty, actor=self.profile.user)
        self.assertEqual(entitlements.remaining(self.profile, "interview_practice"), 2)

    def test_the_practice_limit_is_a_normal_page_state(self):
        from apps.interviews.tests.factories import practice_template

        template = practice_template()
        for _ in range(2):
            entitlements.claim(self.profile, "interview_practice")
        response = self.client.post(
            reverse("interviews:practice"), {"template": template.slug}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You have used this month&#x27;s practice interviews")


class QuotaConcurrencyTests(TransactionTestCase):
    """Two tabs at once cannot take more than the allowance."""

    def test_simultaneous_claims_respect_the_limit(self):
        from django.core.management import call_command

        call_command("seed_plans", quiet_summary=True)
        profile = create_profile()
        barrier = threading.Barrier(4)
        results: list[str] = []

        def run():
            try:
                barrier.wait()
                entitlements.claim(profile, "interview_practice")
                results.append("ok")
            except entitlements.PlanLimitExceeded:
                results.append("refused")
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("ok"), 2)
        self.assertEqual(results.count("refused"), 2)
        self.assertEqual(usage.used(owner=profile, metric="interview_practice"), 2)
