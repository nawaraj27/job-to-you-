"""A failed send is logged without the recipient's address."""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from apps.notifications import email


class FailedSendLoggingTests(SimpleTestCase):
    def test_the_address_is_masked_in_the_log(self):
        error = Exception("550 Recipient address rejected: maya.karki@example.com")
        with (
            mock.patch.object(email, "django_send_mail", side_effect=error),
            self.assertLogs("apps.notifications.email", level="WARNING") as logs,
        ):
            sent = email.send(to="maya.karki@example.com", subject="Hi", body="Body")

        self.assertFalse(sent)
        record = logs.records[0]
        self.assertEqual(record.to, "m***@example.com")
        self.assertNotIn("maya.karki", record.detail)
        self.assertNotIn("maya.karki", str(record.__dict__))

    def test_masking_keeps_only_the_domain(self):
        self.assertEqual(email.masked("ram@example.com"), "r***@example.com")
        self.assertEqual(email.masked("not-an-address"), "***")
