"""
One name, set in one place (docs/BRANDING.md).

The site's name is a working title until a replacement is chosen. These tests
pin the two things that make the eventual rename a configuration change: no
visible template text spells the name out, and setting `SITE_NAME` reaches the
pages, the emails and the CV products' labels. CV features carry no "AI" in
their names; text a model wrote stays labelled as such (SRS §9.3.9).
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.navigation import FOOTER_NAV

TEMPLATES = Path(settings.BASE_DIR) / "templates"
_COMMENT = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|{#.*?#}", re.S)


class BrandingTests(TestCase):
    def test_no_template_spells_out_the_site_name(self):
        offenders = []
        for path in TEMPLATES.rglob("*.html"):
            text = _COMMENT.sub("", path.read_text(encoding="utf-8"))
            # Webhook header names are an API contract, not branding.
            text = text.replace("X-CareerAI-Signature", "")
            if "CareerAI" in text:
                offenders.append(str(path.relative_to(TEMPLATES)))
        self.assertEqual(offenders, [])

    @override_settings(SITE_NAME="Example Careers")
    def test_the_configured_name_reaches_the_pages(self):
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertIn("Example Careers", html)
        footer = html[html.index("<footer") : html.index("</footer>")]
        self.assertIn("Example Careers", footer)

    @override_settings(SITE_NAME="Example Careers", RATELIMIT_ENABLED=False)
    def test_the_configured_name_reaches_emails(self):
        from apps.accounts.tests.factories import create_user

        user = create_user(email="brand@example.com")
        self.client.post(reverse("accounts:password_reset"), {"email": user.email})
        self.assertTrue(mail.outbox)
        self.assertIn("Example Careers", mail.outbox[-1].subject)
        self.assertNotIn("CareerAI", mail.outbox[-1].subject)

    def test_cv_features_are_not_named_after_ai(self):
        for _title, items in FOOTER_NAV:
            for item in items:
                self.assertNotRegex(item.label, r"\bAI\b")
