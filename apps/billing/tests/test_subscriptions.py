"""
The subscription lifecycle, invoicing and settlement (SRS §19, ADR-A15).

The properties this file is answerable for:

  * **One live subscription per owner, enforced by the database.** Two would
    make "which plan am I on" a question with two answers, and the entitlement
    code would silently pick whichever sorted first.
  * **A cancellation is a date.** Somebody who cancels mid-period keeps what
    they paid for until the period ends, and the test asserts the entitlement
    is still there the moment after they click.
  * **Invoicing is idempotent per period, and settlement is idempotent per
    provider reference.** Both are the difference between a billing system and
    a way to charge people twice.
  * **A callback is verified before anything is written.** An unsigned payload
    is not a failed payment; it is not a payment at all.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import (
    Coupon,
    Invoice,
    InvoiceState,
    Payment,
    PaymentState,
    Subscription,
    SubscriptionState,
)
from apps.billing.providers import base as providers
from apps.billing.providers.fake import provider as fake_gateway
from apps.billing.services import coupons, entitlements, invoices, payments, subscriptions
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.companies.tests.factories import company_with_owner


class SubscriptionFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        factories.seed()

    def setUp(self):
        self.profile = create_profile()
        self.company, _ = company_with_owner(create_profile().user)
        fake_gateway.reset()


class LifecycleTests(SubscriptionFixture):
    def test_subscribing_puts_the_owner_on_the_plan(self):
        subscription = factories.put_on_pro(self.profile)

        self.assertEqual(subscription.state, SubscriptionState.ACTIVE)
        self.assertEqual(subscription.candidate, self.profile)
        self.assertIsNone(subscription.company)

    def test_a_second_live_subscription_is_refused_by_the_service(self):
        factories.put_on_pro(self.profile)
        with self.assertRaises(Conflict):
            factories.put_on_pro(self.profile)

    def test_a_second_live_subscription_is_refused_by_the_database(self):
        # The service check is the friendly message; the constraint is the
        # guarantee, and only one of them survives a race.
        factories.put_on_pro(self.profile)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Subscription.objects.create(
                candidate=self.profile,
                plan=factories.pro_plan(),
                current_period_end=timezone.localdate() + dt.timedelta(days=30),
            )

    def test_a_subscription_needs_exactly_one_owner(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Subscription.objects.create(
                candidate=self.profile,
                company=self.company,
                plan=factories.pro_plan(),
                current_period_end=timezone.localdate(),
            )

    def test_a_candidate_cannot_be_put_on_a_company_plan(self):
        with self.assertRaises(InvalidState):
            subscriptions.subscribe(owner=self.profile, plan=factories.employer_plan())

    def test_a_retired_plan_cannot_be_subscribed_to(self):
        plan = factories.pro_plan()
        plan.is_active = False
        plan.save(update_fields=["is_active"])

        with self.assertRaises(InvalidState):
            subscriptions.subscribe(owner=self.profile, plan=plan)

    def test_changing_plan_keeps_the_subscription(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.change_plan(subscription=subscription, plan=factories.free_plan())

        # Decision D-08: Pro -> Free waits for the period end, on the same row.
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan.code, "pro")
        self.assertEqual(subscription.pending_plan.code, "free")
        subscriptions.renew(subscription=subscription)
        subscription.refresh_from_db()
        self.assertEqual(subscription.plan.code, "free")
        self.assertEqual(Subscription.objects.filter(candidate=self.profile).count(), 1)

    def test_changing_to_the_same_plan_is_refused(self):
        subscription = factories.put_on_pro(self.profile)
        with self.assertRaises(InvalidState):
            subscriptions.change_plan(subscription=subscription, plan=factories.pro_plan())

    def test_cancelling_leaves_the_plan_working_until_the_period_ends(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.cancel(subscription=subscription, actor=self.profile.user)

        subscription.refresh_from_db()
        self.assertEqual(subscription.cancel_at, subscription.current_period_end)
        self.assertTrue(subscription.is_live)
        # The property that matters to the person who just clicked it.
        self.assertTrue(entitlements.allows(self.profile, "pro_templates"))

    def test_an_immediate_cancellation_withdraws_now(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.cancel(subscription=subscription, actor=self.profile.user, immediately=True)
        self.assertFalse(entitlements.allows(self.profile, "pro_templates"))

    def test_a_cancellation_can_be_undone_before_it_lands(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.cancel(subscription=subscription)
        subscriptions.resume(subscription=subscription)

        subscription.refresh_from_db()
        self.assertIsNone(subscription.cancel_at)

    def test_changing_plan_clears_a_pending_cancellation(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.cancel(subscription=subscription)
        subscriptions.change_plan(subscription=subscription, plan=factories.free_plan())

        subscription.refresh_from_db()
        self.assertIsNone(subscription.cancel_at)

    def test_the_cancellation_date_ends_it_when_it_arrives(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.cancel(subscription=subscription)
        Subscription.objects.filter(pk=subscription.pk).update(
            cancel_at=timezone.localdate() - dt.timedelta(days=1)
        )

        self.assertEqual(subscriptions.expire_due(), 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, SubscriptionState.CANCELLED)

    def test_an_unpaid_subscription_is_only_ended_after_the_grace_period(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.mark_past_due(subscription=subscription, invoice_number="INV-1")

        self.assertEqual(subscriptions.expire_overdue(), 0)

        Subscription.objects.filter(pk=subscription.pk).update(
            current_period_end=timezone.localdate() - dt.timedelta(days=30)
        )
        self.assertEqual(subscriptions.expire_overdue(), 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, SubscriptionState.EXPIRED)

    def test_renewal_rolls_the_period_forward(self):
        subscription = factories.put_on_pro(self.profile)
        was = subscription.current_period_end
        subscriptions.renew(subscription=subscription)

        subscription.refresh_from_db()
        self.assertEqual(subscription.current_period_start, was)
        self.assertGreater(subscription.current_period_end, was)

    def test_a_month_end_start_date_does_not_overflow(self):
        # 31 January plus a month is 28 February, which is what every provider
        # does and what a naive `day=31` would crash on.
        self.assertEqual(
            subscriptions.next_period_end(dt.date(2025, 1, 31), "month"), dt.date(2025, 2, 28)
        )
        self.assertEqual(
            subscriptions.next_period_end(dt.date(2024, 2, 29), "year"), dt.date(2025, 2, 28)
        )


class InvoiceTests(SubscriptionFixture):
    def setUp(self):
        super().setUp()
        self.subscription = factories.put_on_pro(self.profile)

    def test_an_invoice_is_raised_for_the_period(self):
        invoice = invoices.issue(subscription=self.subscription)

        self.assertEqual(invoice.total, factories.pro_plan().price)
        self.assertEqual(invoice.state, InvoiceState.OPEN)
        self.assertTrue(invoice.number.startswith("INV-"))

    def test_issuing_twice_for_one_period_bills_once(self):
        first = invoices.issue(subscription=self.subscription)
        second = invoices.issue(subscription=self.subscription)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_a_quoted_plan_is_not_invoiced_automatically(self):
        subscription = subscriptions.subscribe(owner=self.company, plan=factories.employer_plan())
        with self.assertRaises(InvalidState):
            invoices.issue(subscription=subscription)

    def test_a_free_plan_invoice_is_already_settled(self):
        # On Free for this period (a downgrade applies at renewal, D-08).
        self.subscription.plan = factories.free_plan()
        self.subscription.save(update_fields=["plan"])
        invoice = invoices.issue(subscription=self.subscription)

        self.assertEqual(invoice.state, InvoiceState.PAID)
        self.assertEqual(invoice.total, Decimal("0"))

    def test_a_percentage_coupon_comes_off_the_total(self):
        coupon = Coupon.objects.create(code="HALF", percent_off=50)
        invoice = invoices.issue(subscription=self.subscription, coupon=coupon)

        half = factories.pro_plan().price / 2
        self.assertEqual(invoice.discount, half)
        self.assertEqual(invoice.total, half)

    def test_an_amount_coupon_cannot_make_a_total_negative(self):
        coupon = Coupon.objects.create(
            code="BIG", amount_off=factories.pro_plan().price + Decimal("500")
        )
        invoice = invoices.issue(subscription=self.subscription, coupon=coupon)

        self.assertEqual(invoice.total, Decimal("0"))

    def test_a_coupon_for_another_plan_is_ignored_silently(self):
        coupon = Coupon.objects.create(
            code="EMPLOYERS", percent_off=50, plan=factories.employer_plan()
        )
        invoice = invoices.issue(subscription=self.subscription, coupon=coupon)
        self.assertEqual(invoice.total, factories.pro_plan().price)

    def test_an_expired_coupon_is_refused_where_a_person_can_see_it(self):
        Coupon.objects.create(
            code="LASTYEAR", percent_off=50, expires_on=timezone.localdate() - dt.timedelta(days=1)
        )
        with self.assertRaises(InvalidState):
            coupons.validate(code="LASTYEAR", plan=factories.pro_plan())

    def test_an_unknown_coupon_says_so(self):
        with self.assertRaises(NotFound):
            coupons.validate(code="NOPE", plan=factories.pro_plan())

    def test_a_fully_redeemed_coupon_stops_working(self):
        coupon = Coupon.objects.create(code="ONCE", percent_off=10, max_redemptions=1)
        coupons.redeem(coupon=coupon)

        with self.assertRaises(InvalidState):
            coupons.validate(code="ONCE", plan=factories.pro_plan())

    def test_a_paid_invoice_cannot_be_voided(self):
        invoice = invoices.issue(subscription=self.subscription)
        invoices.mark_paid(invoice=invoice, provider="manual")

        with self.assertRaises(InvalidState):
            invoices.void(invoice=invoice)

    def test_overdue_finds_only_open_invoices_past_their_date(self):
        invoice = invoices.issue(subscription=self.subscription)
        self.assertEqual(invoices.overdue(), [])

        Invoice.objects.filter(pk=invoice.pk).update(
            due_on=timezone.localdate() - dt.timedelta(days=1)
        )
        self.assertEqual([row.pk for row in invoices.overdue()], [invoice.pk])


class PaymentTests(SubscriptionFixture):
    def setUp(self):
        super().setUp()
        self.subscription = factories.put_on_pro(self.profile)
        self.invoice = invoices.issue(subscription=self.subscription)

    def test_a_verified_callback_settles_the_invoice(self):
        payload = fake_gateway.callback_for(invoice=self.invoice)
        payment = payments.settle_callback(provider_key="fake", payload=payload)

        self.invoice.refresh_from_db()
        self.assertEqual(payment.state, PaymentState.SUCCEEDED)
        self.assertEqual(self.invoice.state, InvoiceState.PAID)

    def test_an_unsigned_callback_writes_nothing(self):
        payload = fake_gateway.callback_for(invoice=self.invoice)
        payload["signature"] = "not-the-signature"

        with self.assertRaises(providers.PaymentUnverified):
            payments.settle_callback(provider_key="fake", payload=payload)
        self.assertEqual(Payment.objects.count(), 0)

    def test_an_edited_amount_fails_verification(self):
        # The signature covers the amount, which is the whole reason it exists.
        payload = fake_gateway.callback_for(invoice=self.invoice)
        payload["amount"] = "0.01"

        with self.assertRaises(providers.PaymentUnverified):
            payments.settle_callback(provider_key="fake", payload=payload)

    def test_a_replayed_callback_does_not_take_the_money_twice(self):
        payload = fake_gateway.callback_for(invoice=self.invoice)
        first = payments.settle_callback(provider_key="fake", payload=payload)
        second = payments.settle_callback(provider_key="fake", payload=payload)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.count(), 1)

    def test_a_declined_payment_leaves_the_invoice_open(self):
        fake_gateway.fail_next = True
        payload = fake_gateway.callback_for(invoice=self.invoice)
        payment = payments.settle_callback(provider_key="fake", payload=payload)

        self.invoice.refresh_from_db()
        self.assertEqual(payment.state, PaymentState.FAILED)
        self.assertEqual(self.invoice.state, InvoiceState.OPEN)

    def test_a_callback_naming_no_invoice_is_refused(self):
        from apps.billing.providers.fake import sign

        payload = fake_gateway.callback_for(invoice=self.invoice)
        payload["invoice"] = "INV-does-not-exist"
        payload["signature"] = sign(payload)

        with self.assertRaises(InvalidState):
            payments.settle_callback(provider_key="fake", payload=payload)

    def test_a_bank_transfer_is_recorded_by_a_person(self):
        payment = payments.record_manual(
            invoice=self.invoice, actor=self.profile.user, reference="BANK-9912"
        )

        self.invoice.refresh_from_db()
        self.assertEqual(payment.provider, "manual")
        self.assertEqual(payment.recorded_by, self.profile.user)
        self.assertEqual(self.invoice.state, InvoiceState.PAID)

    def test_a_bank_transfer_needs_a_reference(self):
        with self.assertRaises(InvalidState):
            payments.record_manual(invoice=self.invoice, actor=self.profile.user, reference="   ")

    def test_the_manual_provider_has_no_callback(self):
        # There is no webhook for a bank transfer, and pretending otherwise
        # would give this provider an unverified path the real ones do not have.
        with self.assertRaises(providers.PaymentUnverified):
            providers.get("manual").verify(payload={"anything": True})

    def test_paying_reactivates_a_past_due_subscription(self):
        subscriptions.mark_past_due(
            subscription=self.subscription, invoice_number=self.invoice.number
        )
        payments.record_manual(invoice=self.invoice, actor=self.profile.user, reference="BANK-1")

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.state, SubscriptionState.ACTIVE)

    def test_an_underpayment_does_not_close_the_invoice(self):
        payments.record_manual(
            invoice=self.invoice,
            actor=self.profile.user,
            reference="BANK-2",
            amount=Decimal("1.00"),
        )

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, InvoiceState.OPEN)

    def test_starting_a_payment_on_a_paid_invoice_is_refused(self):
        invoices.mark_paid(invoice=self.invoice, provider="manual")
        with self.assertRaises(InvalidState):
            payments.start(invoice=self.invoice, provider_key="fake")

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(providers.PaymentRefused):
            payments.start(invoice=self.invoice, provider_key="stripe")
