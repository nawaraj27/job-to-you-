"""
Decision D-12: sign-up does not say whether an address is registered.

The page, the status, the redirect and the session are the same for a new
address and an existing one. Only the email differs, and it goes to the
address's owner. Password reset already answered this way; the two now agree.
"""

from __future__ import annotations

import re

from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import EmailAddress, User
from apps.accounts.tests.factories import create_user

PASSWORD = "Neutral-Answer-2026"


def _stable(content: bytes) -> bytes:
    """The page minus what differs per response anyway: CSP nonces, CSRF tokens."""
    content = re.sub(rb'nonce="[^"]*"', b'nonce=""', content)
    return re.sub(rb'(csrf[a-z_-]*"?\s*(?:content|value)=")[^"]*', rb"\1", content)


class SignUpEnumerationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.existing = create_user(email="taken@example.com", password="Original-Pass-1")
        EmailAddress.objects.create(user=self.existing, email="taken@example.com", is_primary=True)

    def sign_up(self, email, client=None, **extra):
        client = client or self.client_class()
        cache.clear()  # the per-IP sign-up limit is not what this test is about
        response = client.post(
            reverse("accounts:signup"),
            {
                "full_name": "Somebody",
                "email": email,
                "signup_intent": "candidate",
                "password1": PASSWORD,
                "password2": PASSWORD,
                **extra,
            },
        )
        return client, response

    def test_new_and_existing_addresses_get_the_same_answer(self):
        new_client, new = self.sign_up("fresh@example.com")
        old_client, old = self.sign_up("taken@example.com")

        self.assertEqual(new.status_code, old.status_code)
        self.assertEqual(new.status_code, 302)
        self.assertEqual(new["Location"], old["Location"])
        self.assertEqual(new["Location"], reverse("accounts:signup_sent"))

        new_page = new_client.get(new["Location"])
        old_page = old_client.get(old["Location"])
        self.assertEqual(new_page.status_code, old_page.status_code)
        self.assertEqual(_stable(new_page.content), _stable(old_page.content))
        for page in (new_page, old_page):
            self.assertNotContains(page, "already uses")
            self.assertNotContains(page, "already exists")

    def test_neither_answer_signs_anybody_in(self):
        new_client, _ = self.sign_up("fresh@example.com")
        old_client, _ = self.sign_up("taken@example.com")

        for client in (new_client, old_client):
            self.assertNotIn("_auth_user_id", client.session)

    def test_the_existing_account_is_untouched(self):
        self.sign_up("taken@example.com")

        self.assertEqual(User.objects.filter(email__iexact="taken@example.com").count(), 1)
        self.existing.refresh_from_db()
        self.assertTrue(self.existing.check_password("Original-Pass-1"))
        self.assertFalse(self.existing.check_password(PASSWORD))

    def test_case_and_spacing_do_not_open_a_second_account_or_a_different_answer(self):
        _, response = self.sign_up("  TAKEN@Example.com ")

        self.assertEqual(response["Location"], reverse("accounts:signup_sent"))
        self.assertEqual(User.objects.filter(email__iexact="taken@example.com").count(), 1)

    def test_a_form_error_does_not_mention_the_address_either(self):
        _, response = self.sign_up("taken@example.com", password2="Something-Else-9")

        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, "already uses", status_code=400)
        self.assertNotIn("email", response.context["form"].errors)

    def test_the_new_address_is_sent_a_confirmation(self):
        self.sign_up("fresh@example.com")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["fresh@example.com"])
        self.assertIn("/accounts/verify/", mail.outbox[0].body)
        self.assertTrue(User.objects.filter(email="fresh@example.com").exists())

    def test_the_existing_owner_is_told_how_to_get_in(self):
        EmailAddress.objects.filter(user=self.existing).update(verified_at="2026-01-01T00:00Z")
        self.sign_up("taken@example.com")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["taken@example.com"])
        self.assertIn("already has one", message.body)
        self.assertIn(reverse("accounts:login"), message.body)
        self.assertIn(reverse("accounts:password_reset"), message.body)
        self.assertNotIn("/accounts/verify/", message.body)

    def test_an_unconfirmed_existing_owner_gets_a_working_confirmation_link(self):
        self.sign_up("taken@example.com")

        link = re.search(r"http://testserver(/accounts/verify/\S+)", mail.outbox[0].body)
        self.assertIsNotNone(link)
        response = self.client.get(link.group(1))

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        address = EmailAddress.objects.get(user=self.existing, is_primary=True)
        self.assertIsNotNone(address.verified_at)

    def test_notices_to_one_address_are_limited_silently(self):
        client = self.client_class()
        answers = []
        for _ in range(5):
            response = client.post(
                reverse("accounts:signup"),
                {
                    "full_name": "Somebody",
                    "email": "taken@example.com",
                    "signup_intent": "candidate",
                    "password1": PASSWORD,
                    "password2": PASSWORD,
                },
                REMOTE_ADDR=f"10.0.0.{len(answers) + 1}",
            )
            answers.append((response.status_code, response["Location"]))

        self.assertEqual(len(set(answers)), 1)
        self.assertEqual(len(mail.outbox), 3)

    def test_a_deactivated_account_gets_no_email_and_the_same_answer(self):
        self.existing.is_active = False
        self.existing.save(update_fields=["is_active"])

        _, response = self.sign_up("taken@example.com")

        self.assertEqual(response["Location"], reverse("accounts:signup_sent"))
        self.assertEqual(mail.outbox, [])

    def test_password_reset_answers_the_same_way_for_both(self):
        answers = []
        for email in ("taken@example.com", "nobody@example.com"):
            cache.clear()
            client = self.client_class()
            response = client.post(reverse("accounts:password_reset"), {"email": email})
            page = client.get(response["Location"])
            messages = [str(m) for m in page.context["messages"]]
            answers.append((response.status_code, response["Location"], messages))

        self.assertEqual(answers[0], answers[1])


class ConfirmThenSignInTests(TestCase):
    """The legitimate path: sign up, confirm, sign in, land at home or `next`."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_confirming_signed_out_leads_to_sign_in_and_then_home(self):
        self.client.post(
            reverse("accounts:signup"),
            {
                "full_name": "New Person",
                "email": "new.person@example.com",
                "signup_intent": "employer",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        link = re.search(r"http://testserver(/accounts/verify/\S+)", mail.outbox[-1].body)

        confirmed = self.client.get(link.group(1))
        self.assertRedirects(confirmed, reverse("accounts:login"), fetch_redirect_response=False)

        signed_in = self.client.post(
            reverse("accounts:login"),
            {"username": "new.person@example.com", "password": PASSWORD},
        )
        self.assertRedirects(signed_in, reverse("employer:index"), fetch_redirect_response=False)

    def test_next_survives_the_email_round_trip(self):
        target = reverse("settings:security")
        self.client.post(
            reverse("accounts:signup"),
            {
                "full_name": "New Person",
                "email": "new.person@example.com",
                "signup_intent": "candidate",
                "password1": PASSWORD,
                "password2": PASSWORD,
                "next": target,
            },
        )
        link = re.search(r"http://testserver(/accounts/verify/\S+)", mail.outbox[-1].body)

        confirmed = self.client.get(link.group(1))

        self.assertIn("next=%2Fsettings%2Fsecurity%2F", confirmed["Location"])

    def test_an_off_site_next_is_dropped_from_the_link(self):
        self.client.post(
            reverse("accounts:signup"),
            {
                "full_name": "New Person",
                "email": "new.person@example.com",
                "signup_intent": "candidate",
                "password1": PASSWORD,
                "password2": PASSWORD,
                "next": "https://evil.example/steal",
            },
        )
        self.assertNotIn("evil.example", mail.outbox[-1].body)
