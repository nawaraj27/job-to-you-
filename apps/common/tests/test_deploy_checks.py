"""Production must deliver email: `check --deploy` refuses a backend that sends nothing."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from apps.common.checks import check_email_is_delivered


def ids(problems):
    return [problem.id for problem in problems]


class EmailDeliveryCheckTests(SimpleTestCase):
    @override_settings(DEBUG=False, EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_the_console_backend_fails_a_production_deploy(self):
        self.assertEqual(ids(check_email_is_delivered(None)), ["common.E003"])

    @override_settings(DEBUG=False, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_the_memory_backend_fails_a_production_deploy(self):
        self.assertEqual(ids(check_email_is_delivered(None)), ["common.E003"])

    @override_settings(
        DEBUG=False, EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST=""
    )
    def test_smtp_without_a_host_fails(self):
        self.assertEqual(ids(check_email_is_delivered(None)), ["common.E004"])

    @override_settings(
        DEBUG=False,
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="smtp.example.com",
    )
    def test_a_configured_smtp_backend_passes(self):
        self.assertEqual(check_email_is_delivered(None), [])

    @override_settings(DEBUG=False, EMAIL_BACKEND="anymail.backends.postmark.EmailBackend")
    def test_a_provider_backend_passes(self):
        self.assertEqual(check_email_is_delivered(None), [])

    @override_settings(DEBUG=True, EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_development_is_not_checked(self):
        self.assertEqual(check_email_is_delivered(None), [])
