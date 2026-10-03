"""
The billing rules for Free and Pro: decision D-08 as amended by ADR-A59.

  1. Free -> Pro waits for payment (ADR-A59, replacing D-08's "at once"):
     choosing Pro raises its invoice, and Pro starts only when that invoice is
     verified paid. Until then the candidate keeps Free.
  2. An overdue unpaid invoice blocks any new subscription or upgrade, with a
     sentence that says why, however many times it is tried.
  3. Pro starts a new period on the day it is paid for, billed in full. No
     proration.
  4. Pro -> Free waits for the end of the current period, Pro works until then,
     the date is on the screen, and nothing is refunded. The renewal that ends
     the period applies it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.billing import tasks
from apps.billing.models import Invoice, InvoiceState, Subscription, SubscriptionState
from apps.billing.services import entitlements, invoices, subscriptions
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState


class DecisionFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        factories.seed()

    def setUp(self):
        self.profile = create_profile()
        self.user = self.profile.user
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def start(self, code: str):
        return self.client.post(reverse("dashboard:plan_start"), {"plan": code}, follow=True)

    def subscription(self) -> Subscription:
        return Subscription.objects.get(candidate=self.profile, state__in=("active", "past_due"))

    def checkout_invoice(self) -> Invoice:
        return Invoice.objects.get(
            subscription__candidate=self.profile, subscription__state="incomplete"
        )

    def pay(self, invoice: Invoice) -> None:
        """A verified payment: a staff member records the bank transfer."""
        from apps.accounts.tests.factories import create_staff
        from apps.billing.services import payments

        payments.record_manual(
            invoice=invoice, actor=create_staff(), reference=f"bank:{invoice.number}"
        )

    def start_and_pay_pro(self) -> Invoice:
        self.start("pro")
        invoice = self.checkout_invoice()
        self.pay(invoice)
        return invoice

    def make_overdue(self, invoice: Invoice) -> None:
        Invoice.objects.filter(pk=invoice.pk).update(due_on=self.today - dt.timedelta(days=1))


class FreeToProTests(DecisionFixture):
    def test_choosing_pro_raises_the_invoice_and_waits_for_payment(self):
        self.start("pro")

        self.assertEqual(entitlements.plan_for(self.profile).code, "free")
        waiting = Subscription.objects.get(candidate=self.profile, state="incomplete")
        self.assertEqual(waiting.plan.code, "pro")
        invoice = Invoice.objects.get(subscription=waiting)
        self.assertEqual(invoice.state, InvoiceState.OPEN)
        self.assertEqual(invoice.currency, "NPR")

    def test_pro_starts_when_the_invoice_is_paid(self):
        invoice = self.start_and_pay_pro()

        self.assertEqual(entitlements.plan_for(self.profile).code, "pro")
        invoice.refresh_from_db()
        self.assertEqual(invoice.state, InvoiceState.PAID)
        self.assertEqual(self.subscription().state, SubscriptionState.ACTIVE)

    def test_choosing_pro_twice_is_one_checkout_and_one_invoice(self):
        self.start("pro")
        self.start("pro")
        self.assertEqual(
            Subscription.objects.filter(candidate=self.profile, state="incomplete").count(), 1
        )
        self.assertEqual(Invoice.objects.filter(subscription__candidate=self.profile).count(), 1)

    def test_a_new_period_starts_on_the_payment_day_with_a_full_period_invoice(self):
        # On Free, part-way through a period (after an earlier downgrade).
        free = subscriptions.subscribe(owner=self.profile, plan=factories.free_plan())
        Subscription.objects.filter(pk=free.pk).update(
            current_period_start=self.today - dt.timedelta(days=12),
            current_period_end=self.today + dt.timedelta(days=18),
        )

        invoice = self.start_and_pay_pro()

        subscription = self.subscription()
        self.assertEqual(subscription.plan.code, "pro")
        self.assertNotEqual(subscription.pk, free.pk)
        free.refresh_from_db()
        self.assertEqual(free.state, SubscriptionState.CANCELLED)
        self.assertEqual(subscription.current_period_start, self.today)
        self.assertEqual(
            subscription.current_period_end,
            subscriptions.next_period_end(self.today, factories.pro_plan().interval),
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.period_start, self.today)
        self.assertEqual(invoice.period_end, subscription.current_period_end)

    def test_no_proration(self):
        free = subscriptions.subscribe(owner=self.profile, plan=factories.free_plan())
        Subscription.objects.filter(pk=free.pk).update(
            current_period_start=self.today - dt.timedelta(days=20),
            current_period_end=self.today + dt.timedelta(days=10),
        )
        self.start("pro")

        invoice = self.checkout_invoice()
        pro = factories.pro_plan()
        self.assertEqual(invoice.subtotal, pro.price)
        self.assertEqual(invoice.total, pro.price)
        self.assertEqual(invoice.discount, Decimal("0"))
        self.assertEqual(len(invoice.lines), 1)


class ProToFreeTests(DecisionFixture):
    def setUp(self):
        super().setUp()
        self.pro_invoice = self.start_and_pay_pro()
        self.page = self.start("free")

    def test_the_downgrade_is_scheduled_not_applied(self):
        subscription = self.subscription()
        self.assertEqual(subscription.plan.code, "pro")
        self.assertEqual(subscription.pending_plan.code, "free")
        self.assertEqual(entitlements.plan_for(self.profile).code, "pro")
        self.assertTrue(AuditLog.objects.filter(verb="billing.downgrade_scheduled").exists())

    def test_the_screen_shows_the_date_and_that_pro_continues(self):
        end = self.subscription().current_period_end
        date = f"{end.day} {end:%b %Y}"
        self.assertContains(self.page, "You move to Free on")
        page = self.client.get(reverse("dashboard:plan"))
        self.assertContains(page, "Moves to Free on")
        self.assertContains(page, date)
        self.assertContains(page, "keeps working until then")
        self.assertContains(page, "Stay on Pro")

    def test_the_pro_invoice_stands_and_nothing_is_refunded(self):
        self.pro_invoice.refresh_from_db()
        self.assertEqual(self.pro_invoice.state, InvoiceState.PAID)
        self.assertEqual(self.pro_invoice.total, factories.pro_plan().price)
        self.assertEqual(Invoice.objects.filter(subscription=self.subscription()).count(), 1)
        self.assertFalse(self.pro_invoice.payments.filter(state="refunded").exists())

    def test_the_renewal_applies_the_downgrade_and_bills_nothing(self):
        subscription = self.subscription()
        end = subscription.current_period_end
        tasks.renew_due(end.isoformat())

        subscription.refresh_from_db()
        self.assertEqual(subscription.plan.code, "free")
        self.assertIsNone(subscription.pending_plan)
        self.assertEqual(subscription.current_period_start, end)
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")
        self.assertEqual(Invoice.objects.filter(subscription=subscription).count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(verb="billing.plan_changed", metadata__scheduled=True).exists()
        )

    def test_nothing_changes_the_day_before(self):
        subscription = self.subscription()
        tasks.renew_due((subscription.current_period_end - dt.timedelta(days=1)).isoformat())
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan.code, "pro")

    def test_a_past_due_subscription_still_moves_to_free_on_the_date(self):
        subscription = self.subscription()
        Subscription.objects.filter(pk=subscription.pk).update(state=SubscriptionState.PAST_DUE)
        tasks.renew_due(subscription.current_period_end.isoformat())

        subscription.refresh_from_db()
        self.assertEqual(subscription.plan.code, "free")
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")
        self.pro_invoice.refresh_from_db()
        self.assertEqual(self.pro_invoice.state, InvoiceState.PAID)

    def test_staying_on_pro_withdraws_the_downgrade(self):
        self.client.post(reverse("dashboard:plan"), {"action": "keep_plan"})
        subscription = self.subscription()
        self.assertIsNone(subscription.pending_plan)
        tasks.renew_due(subscription.current_period_end.isoformat())
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan.code, "pro")


class OverdueInvoiceTests(DecisionFixture):
    """An unpaid renewal invoice, past its due date."""

    def setUp(self):
        super().setUp()
        self.start_and_pay_pro()
        tasks.renew_due(self.subscription().current_period_end.isoformat())
        self.invoice = Invoice.objects.get(
            subscription__candidate=self.profile, state=InvoiceState.OPEN
        )
        self.make_overdue(self.invoice)

    def test_an_owner_with_an_overdue_invoice_cannot_upgrade(self):
        subscription = self.subscription()
        # Down to Free at the period end, applied; then try Pro again.
        subscriptions.change_plan(subscription=subscription, plan=factories.free_plan())
        tasks.renew_due(subscription.current_period_end.isoformat())
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan.code, "free")

        with self.assertRaises(InvalidState) as caught:
            subscriptions.change_plan(subscription=subscription, plan=factories.pro_plan())
        self.assertIn(self.invoice.number, caught.exception.message)
        self.assertIn("Settle it first", caught.exception.message)
        with self.assertRaises(InvalidState) as caught:
            subscriptions.start_checkout(owner=self.profile, plan=factories.pro_plan())
        self.assertIn("Settle it first", caught.exception.message)

    def test_repeated_subscriptions_after_the_plan_ended_are_refused(self):
        subscription = self.subscription()
        subscriptions.expire(subscription=subscription, reason="unpaid")
        invoices_before = Invoice.objects.filter(subscription__candidate=self.profile).count()

        for _attempt in range(3):
            page = self.start("pro")
            self.assertContains(page, "is still unpaid")
        self.assertFalse(
            Subscription.objects.filter(
                candidate=self.profile, state__in=("active", "incomplete")
            ).exists()
        )
        self.assertEqual(
            Invoice.objects.filter(subscription__candidate=self.profile).count(), invoices_before
        )

    def test_the_screen_explains_before_anybody_presses_a_button(self):
        subscription = self.subscription()
        subscriptions.expire(subscription=subscription, reason="unpaid")

        page = self.client.get(reverse("dashboard:plan"))
        self.assertContains(page, f"Invoice {self.invoice.number} was due on")
        self.assertContains(page, "Settle it first")
        self.assertNotContains(page, "Move to Pro")

    def test_paying_the_invoice_lifts_the_block(self):
        subscription = self.subscription()
        subscriptions.expire(subscription=subscription, reason="unpaid")
        invoices.mark_paid(invoice=self.invoice, provider="manual")

        self.start_and_pay_pro()
        self.assertEqual(self.subscription().plan.code, "pro")

    def test_a_not_yet_due_invoice_does_not_block(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(due_on=self.today)
        self.assertIsNone(invoices.overdue_for(self.profile))

    def test_a_downgrade_is_still_allowed_while_overdue(self):
        subscriptions.change_plan(subscription=self.subscription(), plan=factories.free_plan())
        self.assertEqual(self.subscription().pending_plan.code, "free")
