"""
Outbound webhooks (SRS §5.12, §15.4).

Most of this file is the SSRF guard, because a webhook URL is a user-supplied
address this server fetches, and getting that wrong hands an attacker the cloud
metadata endpoint and everything else inside the perimeter. The guard is
asserted from the failing side, host class by host class.

The signing tests exist because a signature computed from a hash would be
decoration — a receiver verifying it would be verifying nothing. See ADR-A45
and the note on `WebhookEndpoint`.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings

from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.common.errors import InvalidState
from apps.companies.tests.factories import company_with_owner
from apps.notifications.models import (
    Channel,
    DeliveryStatus,
    NotificationDelivery,
    OutboxEvent,
    WebhookDelivery,
    WebhookEndpoint,
)
from apps.notifications.services import webhooks


def _public_dns(*addresses: str):
    """Pretend a hostname resolves to whatever the test needs."""
    return mock.patch(
        "socket.getaddrinfo",
        return_value=[(2, 1, 6, "", (address, 443)) for address in addresses],
    )


@override_settings(DEBUG=False)
class SafeUrlTests(TestCase):
    def test_a_public_https_url_is_accepted(self):
        with _public_dns("93.184.216.34"):
            self.assertTrue(webhooks.require_safe_url("https://example.com/hooks"))

    def test_plain_http_is_refused_in_production(self):
        with _public_dns("93.184.216.34"), self.assertRaises(webhooks.UnsafeWebhookUrl):
            webhooks.require_safe_url("http://example.com/hooks")

    def test_a_non_http_scheme_is_refused(self):
        for url in ("file:///etc/passwd", "gopher://example.com", "ftp://example.com"):
            with self.subTest(url=url), self.assertRaises(webhooks.UnsafeWebhookUrl):
                webhooks.require_safe_url(url)

    def test_a_url_with_no_host_is_refused(self):
        with self.assertRaises(webhooks.UnsafeWebhookUrl):
            webhooks.require_safe_url("https:///hooks")

    def test_loopback_is_refused(self):
        with _public_dns("127.0.0.1"), self.assertRaises(webhooks.UnsafeWebhookUrl):
            webhooks.require_safe_url("https://localhost.example.com/hooks")

    def test_private_ranges_are_refused(self):
        for address in ("10.0.0.5", "192.168.1.10", "172.16.4.2"):
            with (
                self.subTest(address=address),
                _public_dns(address),
                self.assertRaises(webhooks.UnsafeWebhookUrl),
            ):
                webhooks.require_safe_url("https://internal.example.com/hooks")

    def test_the_cloud_metadata_address_is_refused(self):
        # The single most valuable SSRF target on a cloud host.
        with _public_dns("169.254.169.254"), self.assertRaises(webhooks.UnsafeWebhookUrl):
            webhooks.require_safe_url("https://metadata.example.com/hooks")

    def test_ipv6_loopback_is_refused(self):
        with _public_dns("::1"), self.assertRaises(webhooks.UnsafeWebhookUrl):
            webhooks.require_safe_url("https://v6.example.com/hooks")

    def test_a_rebinding_answer_is_refused_on_the_private_address(self):
        # One public answer and one private answer is the classic DNS-rebinding
        # shape. Checking only the first would let it through.
        with _public_dns("93.184.216.34", "10.0.0.5"), self.assertRaises(webhooks.UnsafeWebhookUrl):
            webhooks.require_safe_url("https://rebind.example.com/hooks")

    def test_an_unusual_port_is_refused(self):
        with _public_dns("93.184.216.34"), self.assertRaises(webhooks.UnsafeWebhookUrl):
            webhooks.require_safe_url("https://example.com:22/hooks")

    def test_a_host_that_does_not_resolve_is_refused(self):
        import socket

        with (
            mock.patch("socket.getaddrinfo", side_effect=socket.gaierror),
            self.assertRaises(webhooks.UnsafeWebhookUrl),
        ):
            webhooks.require_safe_url("https://nowhere.invalid/hooks")


@override_settings(DEBUG=False)
class EndpointTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)

    def create(self, url="https://example.com/hooks", **kwargs):
        with _public_dns("93.184.216.34"):
            return webhooks.create_endpoint(
                company=self.company, actor=self.owner, url=url, **kwargs
            )

    def test_creating_returns_a_secret_that_is_not_stored_in_the_clear(self):
        endpoint, secret = self.create()

        self.assertTrue(secret)
        self.assertEqual(endpoint.secret, secret)

        # Encrypted at rest. Read through raw SQL rather than the ORM: the
        # field decrypts on the way out, so a `values_list` would prove nothing
        # about what is actually on disk.
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT secret FROM notifications_webhookendpoint WHERE id = %s", [endpoint.pk]
            )
            stored = cursor.fetchone()[0]

        self.assertNotIn(secret, stored)
        self.assertTrue(stored.startswith("gAAAAA"))  # Fernet token

    def test_creating_is_audited(self):
        self.create()
        self.assertTrue(AuditLog.objects.filter(verb="webhook.endpoint_created").exists())

    def test_an_unsafe_url_is_refused_at_registration(self):
        with _public_dns("10.0.0.5"), self.assertRaises(webhooks.UnsafeWebhookUrl):
            webhooks.create_endpoint(
                company=self.company, actor=self.owner, url="https://internal.example.com/h"
            )

    def test_the_endpoint_count_is_capped(self):
        for index in range(webhooks.MAX_ENDPOINTS_PER_COMPANY):
            self.create(url=f"https://example.com/hooks/{index}")

        with self.assertRaises(InvalidState):
            self.create(url="https://example.com/hooks/one-too-many")

    def test_an_unknown_event_type_is_dropped_rather_than_stored(self):
        # A subscription to a typo is silence, and silence is the hardest
        # integration bug to find.
        endpoint, _ = self.create(event_types=["job.published", "job.exploded"])
        self.assertEqual(endpoint.event_types, ["job.published"])

    def test_an_empty_subscription_means_everything(self):
        endpoint, _ = self.create()
        self.assertTrue(endpoint.wants("anything.at_all"))

    def test_a_subscription_list_filters(self):
        endpoint, _ = self.create(event_types=["job.published"])
        self.assertTrue(endpoint.wants("job.published"))
        self.assertFalse(endpoint.wants("job.closed"))

    def test_rotating_replaces_the_secret(self):
        endpoint, first = self.create()
        second = webhooks.rotate_secret(endpoint=endpoint, actor=self.owner)

        self.assertNotEqual(first, second)
        endpoint.refresh_from_db()
        self.assertEqual(endpoint.secret, second)

    def test_disabling_takes_it_out_of_the_live_set(self):
        endpoint, _ = self.create()
        webhooks.set_active(endpoint=endpoint, actor=self.owner, active=False)

        endpoint.refresh_from_db()
        self.assertFalse(endpoint.is_live)


class SigningTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner)
        with _public_dns("93.184.216.34"), override_settings(DEBUG=False):
            self.endpoint, self.secret = webhooks.create_endpoint(
                company=self.company, actor=self.owner, url="https://example.com/hooks"
            )

    def test_a_signature_verifies_against_the_secret_the_company_holds(self):
        body = b'{"hello":"world"}'
        signature = webhooks.sign(secret=self.secret, timestamp=1000, body=body)

        self.assertTrue(
            webhooks.verify_signature(
                endpoint=self.endpoint, timestamp=1000, body=body, signature=signature
            )
        )

    def test_a_different_body_does_not_verify(self):
        signature = webhooks.sign(secret=self.secret, timestamp=1000, body=b"a")
        self.assertFalse(
            webhooks.verify_signature(
                endpoint=self.endpoint, timestamp=1000, body=b"b", signature=signature
            )
        )

    def test_a_replayed_timestamp_does_not_verify(self):
        # The timestamp is inside the signed material, so a captured request
        # cannot be re-presented later with a fresh one.
        signature = webhooks.sign(secret=self.secret, timestamp=1000, body=b"a")
        self.assertFalse(
            webhooks.verify_signature(
                endpoint=self.endpoint, timestamp=2000, body=b"a", signature=signature
            )
        )

    def test_a_rotated_secret_invalidates_old_signatures(self):
        body = b"a"
        old = webhooks.sign(secret=self.secret, timestamp=1000, body=body)
        webhooks.rotate_secret(endpoint=self.endpoint, actor=self.owner)
        self.endpoint.refresh_from_db()

        self.assertFalse(
            webhooks.verify_signature(
                endpoint=self.endpoint, timestamp=1000, body=body, signature=old
            )
        )


class DispatchTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner)
        with _public_dns("93.184.216.34"), override_settings(DEBUG=False):
            self.endpoint, self.secret = webhooks.create_endpoint(
                company=self.company, actor=self.owner, url="https://example.com/hooks"
            )
        self.event = OutboxEvent.objects.create(
            type="job.published",
            schema_version=1,
            payload={"company_id": self.company.pk, "job_public_id": "abc", "title": "Role"},
        )

    def test_dispatch_queues_one_delivery_per_interested_endpoint(self):
        self.assertEqual(webhooks.dispatch(self.event), 1)
        self.assertEqual(WebhookDelivery.objects.count(), 1)

    def test_dispatching_twice_does_not_queue_twice(self):
        webhooks.dispatch(self.event)
        self.assertEqual(webhooks.dispatch(self.event), 0)
        self.assertEqual(WebhookDelivery.objects.count(), 1)

    def test_an_event_with_no_company_reaches_nobody(self):
        orphan = OutboxEvent.objects.create(type="job.published", schema_version=1, payload={})
        self.assertEqual(webhooks.dispatch(orphan), 0)

    def test_another_companys_endpoint_is_not_reached(self):
        other, other_membership = company_with_owner(create_user(email="them@example.test"))
        with _public_dns("93.184.216.34"), override_settings(DEBUG=False):
            webhooks.create_endpoint(
                company=other, actor=other_membership.user, url="https://other.example.com/h"
            )

        webhooks.dispatch(self.event)
        self.assertEqual(WebhookDelivery.objects.filter(endpoint__company=other).count(), 0)

    def test_a_disabled_endpoint_is_not_reached(self):
        webhooks.set_active(endpoint=self.endpoint, actor=self.owner, active=False)
        self.assertEqual(webhooks.dispatch(self.event), 0)

    def test_a_successful_delivery_is_recorded_and_signed(self):
        webhooks.dispatch(self.event)
        delivery = WebhookDelivery.objects.get()

        captured = {}

        def _post(url, data=None, headers=None, **kwargs):
            captured["headers"] = headers
            captured["body"] = data
            return mock.Mock(status_code=200, text="ok")

        with (
            _public_dns("93.184.216.34"),
            override_settings(DEBUG=False),
            mock.patch("requests.post", side_effect=_post),
        ):
            self.assertTrue(webhooks.attempt(delivery))

        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.delivered_at)
        self.assertEqual(delivery.status_code, 200)

        # The signature is real: it verifies against the company's secret.
        self.assertTrue(
            webhooks.verify_signature(
                endpoint=self.endpoint,
                timestamp=int(captured["headers"]["X-CareerAI-Timestamp"]),
                body=captured["body"],
                signature=captured["headers"]["X-CareerAI-Signature"],
            )
        )

    def test_the_ledger_records_a_successful_webhook(self):
        webhooks.dispatch(self.event)
        delivery = WebhookDelivery.objects.get()

        with (
            _public_dns("93.184.216.34"),
            override_settings(DEBUG=False),
            mock.patch("requests.post", return_value=mock.Mock(status_code=200, text="")),
        ):
            webhooks.attempt(delivery)

        ledger = NotificationDelivery.objects.get(
            event=self.event, endpoint=self.endpoint, channel=Channel.WEBHOOK
        )
        self.assertEqual(ledger.status, DeliveryStatus.SENT)

    def test_a_failure_is_retried_with_backoff(self):
        webhooks.dispatch(self.event)
        delivery = WebhookDelivery.objects.get()

        with (
            _public_dns("93.184.216.34"),
            override_settings(DEBUG=False),
            mock.patch("requests.post", return_value=mock.Mock(status_code=500, text="no")),
        ):
            self.assertFalse(webhooks.attempt(delivery))

        delivery.refresh_from_db()
        self.assertEqual(delivery.attempt, 1)
        self.assertIsNotNone(delivery.next_retry_at)
        self.assertIn("500", delivery.error)

    def test_a_delivery_stops_being_retried_after_the_limit(self):
        webhooks.dispatch(self.event)
        delivery = WebhookDelivery.objects.get()
        WebhookDelivery.objects.filter(pk=delivery.pk).update(attempt=webhooks.MAX_ATTEMPTS - 1)
        delivery.refresh_from_db()

        with (
            _public_dns("93.184.216.34"),
            override_settings(DEBUG=False),
            mock.patch("requests.post", return_value=mock.Mock(status_code=500, text="")),
        ):
            webhooks.attempt(delivery)

        delivery.refresh_from_db()
        self.assertIsNone(delivery.next_retry_at)

    def test_an_endpoint_that_turns_private_is_disabled_rather_than_called(self):
        # DNS repointed after registration — the reason the URL is re-checked
        # at delivery rather than only at registration.
        webhooks.dispatch(self.event)
        delivery = WebhookDelivery.objects.get()

        with (
            _public_dns("10.0.0.5"),
            override_settings(DEBUG=False),
            mock.patch("requests.post") as post,
        ):
            self.assertFalse(webhooks.attempt(delivery))
            post.assert_not_called()

        self.endpoint.refresh_from_db()
        self.assertFalse(self.endpoint.is_live)

    def test_a_transport_error_is_a_retry_not_a_crash(self):
        webhooks.dispatch(self.event)
        delivery = WebhookDelivery.objects.get()

        with (
            _public_dns("93.184.216.34"),
            override_settings(DEBUG=False),
            mock.patch("requests.post", side_effect=OSError("connection reset")),
        ):
            self.assertFalse(webhooks.attempt(delivery))

        delivery.refresh_from_db()
        self.assertIn("connection reset", delivery.error)


class WebhookScreenTests(TestCase):
    """The employer screen, and the tenant boundary around it."""

    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner, slug="acme-co")
        self.client.force_login(self.owner)

    def url(self):
        from django.urls import reverse

        return reverse("employer:webhooks", args=["acme-co"])

    def test_the_screen_opens(self):
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_creating_shows_the_secret_exactly_once(self):
        with _public_dns("93.184.216.34"), override_settings(DEBUG=False):
            self.client.post(self.url(), {"action": "create", "url": "https://example.com/hooks"})

        first = self.client.get(self.url())
        self.assertIsNotNone(first.context["new_secret"])

        # Second load: gone. A page that reprints a signing key on every load
        # is a page somebody screenshots.
        second = self.client.get(self.url())
        self.assertIsNone(second.context["new_secret"])

    def test_an_unsafe_url_is_refused_by_the_screen(self):
        with _public_dns("10.0.0.5"), override_settings(DEBUG=False):
            self.client.post(
                self.url(), {"action": "create", "url": "https://internal.example.com/h"}
            )
        self.assertEqual(WebhookEndpoint.objects.count(), 0)

    def test_another_companys_endpoint_cannot_be_touched(self):
        other, other_membership = company_with_owner(
            create_user(email="them@example.test"), slug="them-co"
        )
        with _public_dns("93.184.216.34"), override_settings(DEBUG=False):
            theirs, _ = webhooks.create_endpoint(
                company=other, actor=other_membership.user, url="https://other.example.com/h"
            )

        response = self.client.post(
            self.url(), {"action": "delete", "endpoint": str(theirs.public_id)}
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(WebhookEndpoint.objects.filter(pk=theirs.pk).exists())

    def test_a_member_without_company_edit_cannot_manage_endpoints(self):
        from apps.companies.permissions import Role
        from apps.companies.tests.factories import add_member

        interviewer = create_user(email="interviewer@example.test")
        add_member(self.company, interviewer, Role.INTERVIEWER)
        self.client.force_login(interviewer)

        with _public_dns("93.184.216.34"), override_settings(DEBUG=False):
            self.client.post(self.url(), {"action": "create", "url": "https://example.com/hooks"})

        self.assertEqual(WebhookEndpoint.objects.count(), 0)
