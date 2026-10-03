"""
Hosted gateways: eSewa ePay v2 and Khalti KPG-2 (ADR-A58).

The gateways are answered through `httpx.MockTransport`, shaped exactly as their
published documentation shows. Nothing here reaches a real gateway: the test
settings block the network, so a call nobody answered fails loudly.

What is asserted, from the failing side where it matters:

  * the request is signed the way eSewa documents (an official test vector);
  * the outcome comes from the server-side status check, never from the
    browser's return trip;
  * forged, replayed, re-pointed and re-priced returns write nothing;
  * duplicates and concurrent checks produce one payment;
  * an unreachable gateway leaves the attempt open, not failed;
  * only the invoice's payer can start a payment or read its result.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import threading
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from django.core.cache import cache
from django.core.checks import run_checks
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.billing.gateways import base as gateways
from apps.billing.gateways import esewa, khalti
from apps.billing.models import (
    AttemptState,
    Invoice,
    InvoiceState,
    Payment,
    PaymentAttempt,
    PaymentState,
    SubscriptionState,
)
from apps.billing.providers.base import PaymentUnverified
from apps.billing.services import checkout, invoices, subscriptions
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner

#: eSewa's published UAT key and product code (developer.esewa.com.np, "Epay").
#: A public test vector, not a credential: it is what the documentation's own
#: example signatures are computed with.
ESEWA_UAT_SECRET = "8gBm/:&EnhH.1/q"  # noqa: S105

GATEWAYS = {
    "esewa": {
        "enabled": True,
        "product_code": "EPAYTEST",
        "secret_key": ESEWA_UAT_SECRET,
        "form_url": "https://rc-epay.esewa.com.np/api/epay/main/v2/form",
        "status_url": "https://rc.esewa.com.np/api/epay/transaction/status/",
    },
    "khalti": {
        "enabled": True,
        "secret_key": "test-khalti-key",
        "base_url": "https://dev.khalti.com/api/v2/",
    },
}


class FakeGatewayServer:
    """Answers as eSewa's status API and Khalti's initiate/lookup do."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.esewa: dict[str, dict[str, Any]] = {}
        self.esewa_http_status = 200
        self.khalti_initiate: dict[str, Any] | None = None
        self.khalti_initiate_status = 200
        self.khalti_lookup: dict[str, dict[str, Any]] = {}
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.down:
            raise httpx.ConnectTimeout("gateway down", request=request)
        url = str(request.url)
        if "rc.esewa.com.np/api/epay/transaction/status" in url:
            query = parse_qs(urlsplit(url).query)
            uuid = query["transaction_uuid"][0]
            if self.esewa_http_status != 200:
                return httpx.Response(self.esewa_http_status, text="<html>error</html>")
            answer = self.esewa.get(uuid)
            if answer is None:
                return httpx.Response(
                    200,
                    json={
                        "product_code": query["product_code"][0],
                        "transaction_uuid": uuid,
                        "total_amount": float(query["total_amount"][0]),
                        "status": "NOT_FOUND",
                        "ref_id": None,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "product_code": query["product_code"][0],
                    "transaction_uuid": uuid,
                    "total_amount": float(query["total_amount"][0]),
                    "ref_id": "0001TS9",
                    **answer,
                },
            )
        if url.endswith("/epayment/initiate/"):
            assert request.headers["Authorization"] == "Key test-khalti-key"
            if self.khalti_initiate_status != 200:
                return httpx.Response(
                    self.khalti_initiate_status, json={"detail": "Invalid token."}
                )
            body = json.loads(request.content)
            self.khalti_initiate = body
            return httpx.Response(
                200,
                json={
                    "pidx": f"pidx-{body['purchase_order_id']}",
                    "payment_url": f"https://test-pay.khalti.com/?pidx=pidx-{body['purchase_order_id']}",
                    "expires_at": "2026-10-01T16:26:16.471649+05:45",
                    "expires_in": 1800,
                },
            )
        if url.endswith("/epayment/lookup/"):
            pidx = json.loads(request.content)["pidx"]
            answer = self.khalti_lookup.get(pidx)
            if answer is None:
                return httpx.Response(404, json={"detail": "Not found."})
            return httpx.Response(200, json={"pidx": pidx, "fee": 0, "refunded": False, **answer})
        return httpx.Response(599, text="unexpected call")

    def status_calls(self) -> int:
        return sum("transaction/status" in str(c.url) or "lookup" in str(c.url) for c in self.calls)


def esewa_return_data(
    attempt: PaymentAttempt,
    *,
    status: str = "COMPLETE",
    amount: str | None = None,
    uuid: str | None = None,
    secret: str = ESEWA_UAT_SECRET,
    product_code: str = "EPAYTEST",
) -> str:
    """The base64 `data` eSewa appends to the success URL, signed as eSewa signs it."""
    body = {
        "transaction_code": "000AWEO",
        "status": status,
        "total_amount": amount or f"{attempt.amount:.1f}",
        "transaction_uuid": uuid or attempt.reference,
        "product_code": product_code,
        "signed_field_names": "transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names",
    }
    names = body["signed_field_names"].split(",")
    body["signature"] = esewa.sign(esewa.signed_message(body, names), secret)
    return base64.b64encode(json.dumps(body).encode()).decode()


@override_settings(PAYMENT_GATEWAYS=GATEWAYS, PAYMENTS_ENVIRONMENT="test")
class GatewayFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        factories.seed()
        cls.npr_plan = factories.make_plan(code="pro-npr", price=Decimal("1200.00"), currency="NPR")

    def setUp(self):
        cache.clear()
        self.server = FakeGatewayServer()
        gateways.set_transport(lambda: httpx.MockTransport(self.server))
        self.addCleanup(gateways.set_transport, lambda: None)
        self.profile = create_profile()
        self.user = self.profile.user
        self.subscription = subscriptions.subscribe(owner=self.profile, plan=self.npr_plan)
        self.invoice = invoices.issue(subscription=self.subscription)
        self.client.force_login(self.user)

    def pay(self, gateway: str = "esewa", invoice: Invoice | None = None):
        invoice = invoice or self.invoice
        return self.client.post(reverse("billing:pay", args=[invoice.number]), {"gateway": gateway})

    def started(self, gateway: str = "esewa") -> PaymentAttempt:
        response = self.pay(gateway)
        self.assertEqual(response.status_code, 200, response.content[:400])
        return PaymentAttempt.objects.get(
            invoice=self.invoice, provider=gateway, state=AttemptState.INITIATED
        )

    def esewa_return(self, attempt: PaymentAttempt, data: str, *, reference: str | None = None):
        url = reverse("billing:return", args=["esewa", reference or attempt.reference])
        return self.client.get(url, {"data": data})

    def assert_nothing_paid(self) -> None:
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, InvoiceState.OPEN)
        self.assertFalse(Payment.objects.filter(invoice=self.invoice).exists())


class EsewaSigningTests(TestCase):
    def test_the_documented_response_signature_verifies(self):
        # The success response printed in eSewa's ePay v2 documentation, with
        # its signature, checked by the code that checks real responses.
        body = {
            "transaction_code": "000AWEO",
            "status": "COMPLETE",
            "total_amount": "1000.0",
            "transaction_uuid": "250610-162413",
            "product_code": "EPAYTEST",
            "signed_field_names": "transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names",
        }
        names = body["signed_field_names"].split(",")
        self.assertEqual(
            esewa.sign(esewa.signed_message(body, names), ESEWA_UAT_SECRET),
            "62GcfZTmVkzhtUeh+QJ1AqiJrjoWWGof3U+eTPTZ7fA=",
        )

    def test_numbers_are_verified_as_the_literal_text_esewa_signed(self):
        raw = base64.b64encode(b'{"total_amount": 1000.0, "status": "COMPLETE"}').decode()
        self.assertEqual(esewa.EsewaGateway.decode(raw)["total_amount"], "1000.0")

    def test_amount_formatting_is_stable(self):
        self.assertEqual(esewa.format_amount(Decimal("1200.00")), "1200")
        self.assertEqual(esewa.format_amount(Decimal("1200.50")), "1200.5")
        self.assertEqual(esewa.parse_amount("1,200.0"), Decimal("1200"))


class EsewaCheckoutTests(GatewayFixture):
    def test_starting_a_payment_writes_the_attempt_and_signs_the_form(self):
        response = self.pay()

        self.assertEqual(response.status_code, 200)
        attempt = PaymentAttempt.objects.get(invoice=self.invoice)
        self.assertEqual(attempt.amount, Decimal("1200.00"))
        self.assertEqual(attempt.environment, "test")
        self.assertEqual(attempt.state, AttemptState.INITIATED)
        fields = response.context["checkout"].fields
        self.assertEqual(fields["total_amount"], "1200")
        self.assertEqual(fields["transaction_uuid"], attempt.reference)
        self.assertEqual(fields["product_code"], "EPAYTEST")
        self.assertEqual(fields["signed_field_names"], "total_amount,transaction_uuid,product_code")
        expected = esewa.sign(
            f"total_amount=1200,transaction_uuid={attempt.reference},product_code=EPAYTEST",
            ESEWA_UAT_SECRET,
        )
        self.assertEqual(fields["signature"], expected)
        self.assertTrue(
            fields["success_url"].endswith(f"/billing/return/esewa/{attempt.reference}/")
        )
        self.assertTrue(
            fields["failure_url"].endswith(f"/billing/return/esewa/{attempt.reference}/failed/")
        )
        self.assertContains(response, 'action="https://rc-epay.esewa.com.np/api/epay/main/v2/form"')
        self.assertContains(response, "No real money moves")
        # The hand-off page alone may post to eSewa; no secret is on it.
        self.assertIn(
            "form-action 'self' https://rc-epay.esewa.com.np", response["Content-Security-Policy"]
        )
        self.assertNotContains(response, ESEWA_UAT_SECRET)

    def test_a_confirmed_payment_settles_the_invoice(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}

        response = self.esewa_return(attempt, esewa_return_data(attempt))

        self.assertRedirects(response, reverse("billing:attempt", args=[attempt.public_id]))
        attempt.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)
        self.assertEqual(attempt.gateway_ref, "0001TS9")
        self.assertEqual(self.invoice.state, InvoiceState.PAID)
        payment = Payment.objects.get(invoice=self.invoice)
        self.assertEqual(payment.state, PaymentState.SUCCEEDED)
        self.assertEqual(payment.amount, Decimal("1200.00"))
        self.assertEqual(payment.provider, "esewa")
        self.assertEqual(attempt.payment, payment)
        page = self.client.get(response["Location"])
        self.assertContains(page, "Payment received")
        self.assertContains(page, "0001TS9")

    def test_a_duplicate_callback_records_one_payment_without_asking_again(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}
        data = esewa_return_data(attempt)

        self.esewa_return(attempt, data)
        checks = self.server.status_calls()
        second = self.esewa_return(attempt, data)

        self.assertEqual(second.status_code, 302)
        self.assertEqual(Payment.objects.filter(invoice=self.invoice).count(), 1)
        self.assertEqual(self.server.status_calls(), checks)

    def test_refresh_and_back_only_reread_the_result(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}
        result = self.esewa_return(attempt, esewa_return_data(attempt))["Location"]
        checks = self.server.status_calls()

        for _ in range(3):
            self.assertEqual(self.client.get(result).status_code, 200)

        self.assertEqual(self.server.status_calls(), checks)
        self.assertEqual(Payment.objects.count(), 1)

    def test_a_signed_success_the_status_api_does_not_confirm_is_not_a_payment(self):
        # The browser says COMPLETE; eSewa's server has no such payment.
        attempt = self.started()

        self.esewa_return(attempt, esewa_return_data(attempt))

        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.PENDING)
        self.assert_nothing_paid()

    def test_an_invalid_signature_is_refused_and_writes_nothing(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}

        response = self.esewa_return(attempt, esewa_return_data(attempt, secret="not-the-key"))

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "valid signature", status_code=400)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.INITIATED)
        self.assertEqual(attempt.checks, 0)
        self.assert_nothing_paid()
        self.assertEqual(self.server.status_calls(), 0)

    def test_garbage_data_is_refused(self):
        attempt = self.started()
        response = self.esewa_return(attempt, "not-base64-json!!")
        self.assertEqual(response.status_code, 400)
        self.assert_nothing_paid()

    def test_a_different_amount_is_refused_even_when_signed(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}

        response = self.esewa_return(attempt, esewa_return_data(attempt, amount="12.0"))

        self.assertEqual(response.status_code, 400)
        self.assert_nothing_paid()

    def test_an_unknown_reference_is_refused(self):
        attempt = self.started()
        response = self.client.get(
            reverse("billing:return", args=["esewa", "ca-0000"]),
            {"data": esewa_return_data(attempt, uuid="ca-0000")},
        )
        self.assertEqual(response.status_code, 400)
        self.assert_nothing_paid()

    def test_a_return_replayed_against_another_attempt_is_refused(self):
        first = self.started()
        self.server.esewa[first.reference] = {"status": "COMPLETE"}
        data = esewa_return_data(first)
        # A second attempt on another invoice, by the same person.
        other_sub = subscriptions.subscribe(owner=create_profile(), plan=self.npr_plan)
        other_invoice = invoices.issue(subscription=other_sub)
        second = PaymentAttempt.objects.create(
            invoice=other_invoice,
            provider="esewa",
            reference="ca-second",
            amount=Decimal("1200.00"),
            currency="NPR",
        )

        response = self.esewa_return(second, data, reference=second.reference)

        self.assertEqual(response.status_code, 400)
        second.refresh_from_db()
        self.assertEqual(second.state, AttemptState.INITIATED)
        self.assertFalse(Payment.objects.exists())

    def test_another_merchants_response_is_refused(self):
        attempt = self.started()
        response = self.esewa_return(attempt, esewa_return_data(attempt, product_code="OTHERSHOP"))
        self.assertEqual(response.status_code, 400)

    def test_cancelling_at_esewa_closes_the_attempt(self):
        attempt = self.started()

        response = self.client.get(
            reverse("billing:return_failed", args=["esewa", attempt.reference])
        )

        self.assertEqual(response.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.CANCELLED)
        self.assert_nothing_paid()
        page = self.client.get(response["Location"])
        self.assertContains(page, "Payment cancelled")
        self.assertContains(page, "No money was taken")

    def test_a_failure_return_does_not_override_a_payment_esewa_completed(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}

        self.client.get(reverse("billing:return_failed", args=["esewa", attempt.reference]))

        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)

    def test_esewa_canceled_status_cancels(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "CANCELED"}
        self.esewa_return(attempt, esewa_return_data(attempt, status="CANCELED"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.CANCELLED)
        self.assert_nothing_paid()

    def test_a_cancelled_attempt_that_is_later_paid_still_settles(self):
        attempt = self.started()
        self.client.get(reverse("billing:return_failed", args=["esewa", attempt.reference]))
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}

        self.esewa_return(attempt, esewa_return_data(attempt))

        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, InvoiceState.PAID)

    def test_a_delayed_confirmation_is_picked_up_by_the_background_check(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "PENDING"}
        self.esewa_return(attempt, esewa_return_data(attempt))
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.PENDING)
        self.assert_nothing_paid()

        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}
        checked = checkout.reconcile_open(now=timezone.now() + dt.timedelta(minutes=5))

        self.assertEqual(checked, 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, InvoiceState.PAID)

    def test_check_again_asks_the_gateway(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "AMBIGUOUS"}
        self.esewa_return(attempt, esewa_return_data(attempt))
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}

        response = self.client.post(reverse("billing:attempt", args=[attempt.public_id]))

        self.assertEqual(response.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)

    def test_an_unreachable_gateway_leaves_the_attempt_open(self):
        attempt = self.started()
        self.server.down = True

        response = self.esewa_return(attempt, esewa_return_data(attempt))

        self.assertEqual(response.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.PENDING)
        self.assertIn("ConnectTimeout", attempt.failure_reason)
        self.assert_nothing_paid()
        page = self.client.get(response["Location"])
        self.assertContains(page, "Waiting for confirmation")
        self.assertContains(page, "pay again")

        self.server.down = False
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}
        checkout.reconcile_open(now=timezone.now() + dt.timedelta(minutes=5))
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)

    def test_a_failed_status_request_is_not_a_failed_payment(self):
        attempt = self.started()
        self.server.esewa_http_status = 502

        self.esewa_return(attempt, esewa_return_data(attempt))

        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.PENDING)
        self.assertIn("502", attempt.failure_reason)

    def test_a_different_amount_from_the_status_api_is_held_for_review(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE", "total_amount": 12.0}

        self.esewa_return(attempt, esewa_return_data(attempt))

        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.PENDING)
        self.assertIn("staff review", attempt.failure_reason)
        self.assert_nothing_paid()

    def test_a_refund_at_esewa_is_reflected(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}
        self.esewa_return(attempt, esewa_return_data(attempt))
        self.server.esewa[attempt.reference] = {"status": "FULL_REFUND"}

        checkout.reconcile(PaymentAttempt.objects.get(pk=attempt.pk))

        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.REFUNDED)
        self.assertEqual(attempt.payment.state, PaymentState.REFUNDED)

    def test_an_abandoned_attempt_expires(self):
        attempt = self.started()
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=1)
        )

        checkout.reconcile_open()

        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.EXPIRED)

    def test_a_payment_being_confirmed_blocks_a_second_one(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "PENDING"}
        self.esewa_return(attempt, esewa_return_data(attempt))

        response = self.pay()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PaymentAttempt.objects.filter(invoice=self.invoice).count(), 1)
        plan = self.client.get(reverse("dashboard:plan"))
        self.assertContains(plan, "Payment being confirmed")

    def test_a_paid_invoice_cannot_be_paid_again(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}
        self.esewa_return(attempt, esewa_return_data(attempt))

        response = self.pay()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PaymentAttempt.objects.filter(invoice=self.invoice).count(), 1)

    def test_paying_an_already_paid_invoice_twice_records_the_money_and_flags_a_refund(self):
        # Two tabs: both reached eSewa before either came back.
        first = self.started()
        second = PaymentAttempt.objects.create(
            invoice=self.invoice,
            provider="esewa",
            reference="ca-tab-two",
            amount=Decimal("1200.00"),
            currency="NPR",
        )
        self.server.esewa[first.reference] = {"status": "COMPLETE"}
        self.server.esewa[second.reference] = {"status": "COMPLETE"}

        self.esewa_return(first, esewa_return_data(first))
        self.esewa_return(second, esewa_return_data(second))

        self.assertEqual(Payment.objects.filter(invoice=self.invoice).count(), 2)
        second.refresh_from_db()
        self.assertEqual(second.state, AttemptState.SUCCEEDED)
        self.assertIn("refund is owed", second.failure_reason)
        self.assertTrue(second.payment.detail["refund_owed"])

    def test_paying_reactivates_a_past_due_subscription(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(
            due_on=timezone.localdate() - dt.timedelta(days=3)
        )
        self.subscription.state = SubscriptionState.PAST_DUE
        self.subscription.save(update_fields=["state"])
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}

        self.esewa_return(attempt, esewa_return_data(attempt))

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.state, SubscriptionState.ACTIVE)

    def test_two_checks_of_one_attempt_record_one_payment(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}
        stale_a = PaymentAttempt.objects.get(pk=attempt.pk)
        stale_b = PaymentAttempt.objects.get(pk=attempt.pk)

        checkout.reconcile(stale_a)
        checkout.reconcile(stale_b)

        self.assertEqual(Payment.objects.count(), 1)


class AccessTests(GatewayFixture):
    def test_another_persons_invoice_cannot_be_paid(self):
        self.client.force_login(create_profile().user)
        self.assertEqual(self.pay().status_code, 404)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_an_unknown_invoice_is_a_404(self):
        response = self.client.post(
            reverse("billing:pay", args=["INV-000000-999999"]), {"gateway": "esewa"}
        )
        self.assertEqual(response.status_code, 404)

    def test_signed_out_people_are_sent_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.pay().status_code, 302)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_another_persons_payment_page_is_a_404(self):
        attempt = self.started()
        self.client.force_login(create_profile().user)
        url = reverse("billing:attempt", args=[attempt.public_id])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_paying_needs_a_get_free_post(self):
        response = self.client.get(reverse("billing:pay", args=[self.invoice.number]))
        self.assertEqual(response.status_code, 405)

    def test_an_unknown_gateway_is_refused(self):
        response = self.pay("paypal")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_a_usd_invoice_is_not_offered_to_nepali_wallets(self):
        usd_plan = factories.make_plan(code="pro-usd", price=Decimal("12"), currency="USD")
        usd_sub = subscriptions.subscribe(owner=create_profile(), plan=usd_plan)
        usd_invoice = invoices.issue(subscription=usd_sub)
        self.assertEqual(checkout.offered(usd_invoice), [])

    def test_the_plan_page_offers_configured_gateways(self):
        response = self.client.get(reverse("dashboard:plan"))
        self.assertContains(response, ' with eSewa"')
        self.assertContains(response, ' with Khalti"')
        self.assertContains(response, "test mode")

    @override_settings(PAYMENT_GATEWAYS={"esewa": {"enabled": False}, "khalti": {"enabled": False}})
    def test_no_buttons_when_no_gateway_is_enabled(self):
        response = self.client.get(reverse("dashboard:plan"))
        self.assertNotContains(response, "Pay with")
        self.assertContains(response, "By bank transfer")
        self.assertEqual(self.pay().status_code, 302)
        self.assertFalse(PaymentAttempt.objects.exists())


class CompanyAccessTests(GatewayFixture):
    def setUp(self):
        super().setUp()
        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        plan = factories.make_plan(
            code="emp-npr", audience="company", price=Decimal("5000"), currency="NPR"
        )
        sub = subscriptions.subscribe(owner=self.company, plan=plan)
        self.company_invoice = invoices.issue(subscription=sub)

    def test_the_owner_may_pay(self):
        self.client.force_login(self.owner)
        response = self.pay(invoice=self.company_invoice)
        self.assertEqual(response.status_code, 200)
        billing = self.client.get(reverse("employer:billing", args=[self.company.slug]))
        self.assertContains(billing, ' with eSewa"')

    def test_a_company_admin_may_not_move_money(self):
        admin = create_profile().user
        add_member(self.company, admin, role=Role.ADMIN)
        self.client.force_login(admin)

        self.assertEqual(self.pay(invoice=self.company_invoice).status_code, 404)
        billing = self.client.get(reverse("employer:billing", args=[self.company.slug]))
        self.assertNotContains(billing, "Pay with")


class KhaltiCheckoutTests(GatewayFixture):
    def test_starting_calls_initiate_in_paisa_and_hands_over_the_link(self):
        response = self.pay("khalti")

        self.assertEqual(response.status_code, 200)
        attempt = PaymentAttempt.objects.get(invoice=self.invoice)
        self.assertEqual(self.server.khalti_initiate["amount"], 120000)
        self.assertEqual(self.server.khalti_initiate["purchase_order_id"], attempt.reference)
        self.assertTrue(
            self.server.khalti_initiate["return_url"].endswith(
                f"/billing/return/khalti/{attempt.reference}/"
            )
        )
        self.assertEqual(attempt.gateway_token, f"pidx-{attempt.reference}")
        self.assertContains(response, "https://test-pay.khalti.com/?pidx=")
        self.assertNotContains(response, "test-khalti-key")

    def test_a_completed_lookup_settles(self):
        attempt = self.started("khalti")
        self.server.khalti_lookup[attempt.gateway_token] = {
            "status": "Completed",
            "total_amount": 120000,
            "transaction_id": "GFq9PFS7b2iYvL8Lir9oXe",
        }

        response = self.client.get(
            reverse("billing:return", args=["khalti", attempt.reference]),
            {
                "pidx": attempt.gateway_token,
                "status": "Completed",
                "purchase_order_id": attempt.reference,
            },
        )

        self.assertEqual(response.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)
        self.assertEqual(attempt.gateway_ref, "GFq9PFS7b2iYvL8Lir9oXe")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.state, InvoiceState.PAID)

    def test_the_callback_status_is_not_believed(self):
        attempt = self.started("khalti")
        self.server.khalti_lookup[attempt.gateway_token] = {
            "status": "Initiated",
            "total_amount": 120000,
        }

        self.client.get(
            reverse("billing:return", args=["khalti", attempt.reference]),
            {"pidx": attempt.gateway_token, "status": "Completed"},
        )

        self.assert_nothing_paid()

    def test_a_pidx_from_another_payment_is_refused(self):
        attempt = self.started("khalti")
        response = self.client.get(
            reverse("billing:return", args=["khalti", attempt.reference]), {"pidx": "someone-elses"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.status_calls(), 0)

    def test_user_cancelled(self):
        attempt = self.started("khalti")
        self.server.khalti_lookup[attempt.gateway_token] = {
            "status": "User canceled",
            "total_amount": 120000,
        }
        self.client.get(
            reverse("billing:return", args=["khalti", attempt.reference]),
            {"pidx": attempt.gateway_token, "status": "User canceled"},
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.CANCELLED)
        self.assert_nothing_paid()

    def test_expired(self):
        attempt = self.started("khalti")
        self.server.khalti_lookup[attempt.gateway_token] = {
            "status": "Expired",
            "total_amount": 120000,
        }
        checkout.reconcile(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.EXPIRED)

    def test_a_refused_initiation_fails_the_attempt_and_says_so(self):
        self.server.khalti_initiate_status = 401

        response = self.pay("khalti")

        self.assertRedirects(response, reverse("dashboard:plan"), fetch_redirect_response=False)
        attempt = PaymentAttempt.objects.get(invoice=self.invoice)
        self.assertEqual(attempt.state, AttemptState.FAILED)
        self.assertIn("401", attempt.failure_reason)

    def test_an_amount_below_khaltis_floor_is_refused(self):
        tiny = factories.make_plan(code="tiny", price=Decimal("5"), currency="NPR")
        sub = subscriptions.subscribe(owner=create_profile(), plan=tiny)
        invoice = invoices.issue(subscription=sub)
        attempt = PaymentAttempt(invoice=invoice, reference="x", amount=Decimal("5"))
        with self.assertRaises(PaymentUnverified):
            khalti.gateway.begin(
                attempt=attempt, urls=gateways.ReturnUrls("https://a/", "https://a/", "https://a/")
            )


class ConfigurationCheckTests(TestCase):
    def ids(self):
        return {e.id for e in run_checks(tags=["billing"])}

    @override_settings(PAYMENTS_ENVIRONMENT="live")
    def test_live_mode_is_refused(self):
        self.assertIn("billing.E001", self.ids())

    @override_settings(
        PAYMENT_GATEWAYS={
            "esewa": {**GATEWAYS["esewa"], "secret_key": ""},
            "khalti": {"enabled": False},
        }
    )
    def test_an_enabled_gateway_without_its_key_is_refused(self):
        self.assertIn("billing.E002", self.ids())

    @override_settings(
        PAYMENT_GATEWAYS={
            "khalti": {**GATEWAYS["khalti"], "base_url": "https://khalti.com/api/v2/"},
            "esewa": {"enabled": False},
        }
    )
    def test_a_live_host_in_test_mode_is_refused(self):
        self.assertIn("billing.E003", self.ids())

    @override_settings(PAYMENT_GATEWAYS=GATEWAYS)
    def test_the_sandbox_configuration_passes(self):
        self.assertEqual(self.ids(), set())

    @override_settings(PAYMENT_GATEWAYS={"esewa": {"enabled": False}, "khalti": {"enabled": False}})
    def test_disabled_gateways_are_not_offered(self):
        self.assertEqual(gateways.enabled(), [])


@override_settings(PAYMENT_GATEWAYS=GATEWAYS, PAYMENTS_ENVIRONMENT="test")
class ConcurrentVerificationTests(TransactionTestCase):
    """Two callbacks for one attempt at the same moment record one payment."""

    def test_simultaneous_returns(self):
        call_command("seed_plans", quiet_summary=True)
        server = FakeGatewayServer()
        gateways.set_transport(lambda: httpx.MockTransport(server))
        self.addCleanup(gateways.set_transport, lambda: None)
        plan = factories.make_plan(code="pro-npr-c", price=Decimal("1200.00"), currency="NPR")
        sub = subscriptions.subscribe(owner=create_profile(), plan=plan)
        invoice = invoices.issue(subscription=sub)
        attempt = PaymentAttempt.objects.create(
            invoice=invoice,
            provider="esewa",
            reference="ca-race",
            amount=Decimal("1200.00"),
            currency="NPR",
        )
        server.esewa["ca-race"] = {"status": "COMPLETE"}
        data = esewa_return_data(attempt)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def run():
            try:
                barrier.wait()
                checkout.handle_return(
                    gateway_key="esewa", reference="ca-race", params={"data": data}, failed=False
                )
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(Payment.objects.filter(invoice=invoice).count(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)


class ScheduleTests(TestCase):
    def test_open_payments_are_rechecked_on_a_schedule(self):
        from config.celery import app

        tasks = {entry["task"] for entry in app.conf.beat_schedule.values()}
        self.assertIn("apps.billing.tasks.reconcile_payments", tasks)


class PaymentAdminTests(GatewayFixture):
    def setUp(self):
        super().setUp()
        from apps.accounts.tests.factories import create_staff

        self.staff = create_staff(is_superuser=True)

    def test_staff_see_the_environment_and_gateway_state_but_no_secret(self):
        attempt = self.started()
        self.client.force_login(self.staff)

        page = self.client.get(reverse("admin:billing_paymentattempt_changelist"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Payments environment: TEST")
        self.assertContains(page, attempt.reference)
        self.assertNotContains(page, ESEWA_UAT_SECRET)
        self.assertNotContains(page, "test-khalti-key")
        detail = self.client.get(reverse("admin:billing_paymentattempt_change", args=[attempt.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, self.invoice.number)

    def test_attempts_are_read_only(self):
        attempt = self.started()
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.get(reverse("admin:billing_paymentattempt_add")).status_code, 403
        )
        response = self.client.post(
            reverse("admin:billing_paymentattempt_delete", args=[attempt.pk]), {"post": "yes"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(PaymentAttempt.objects.filter(pk=attempt.pk).exists())

    def test_staff_can_recheck_with_the_gateway(self):
        attempt = self.started()
        self.server.esewa[attempt.reference] = {"status": "COMPLETE"}
        self.client.force_login(self.staff)

        self.client.post(
            reverse("admin:billing_paymentattempt_changelist"),
            {"action": "recheck", "_selected_action": [attempt.pk]},
        )

        attempt.refresh_from_db()
        self.assertEqual(attempt.state, AttemptState.SUCCEEDED)

    def test_is_staff_alone_sees_no_payments(self):
        from apps.accounts.tests.factories import create_staff

        self.client.force_login(create_staff())
        self.assertEqual(
            self.client.get(reverse("admin:billing_paymentattempt_changelist")).status_code, 403
        )
