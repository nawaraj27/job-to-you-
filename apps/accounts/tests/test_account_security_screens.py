"""
Account security screens (SRS FR-1.2 to FR-1.5, AUTH findings of the whole-site loop).

Every service here existed and had no screen: password reset was a Must with
no route, two-factor was mandatory for staff with no way to turn it on, and a
revoked session carried on working from the Redis cache. These tests drive the
screens end to end with the test client.
"""

from __future__ import annotations

import datetime as dt
import re
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import EmailAddress, StaffRole, UserSession
from apps.accounts.services import identity, mfa
from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog

PASSWORD = "test-password-not-a-secret"
NEW_PASSWORD = "Himalaya-Ledger-2026!"


def _code(device: TOTPDevice) -> str:
    return f"{totp(device.bin_key, device.step, device.t0, device.digits, device.drift):06d}"


def _sign_in(client, user, password=PASSWORD):
    # Every test client comes from 127.0.0.1, and this module signs in more
    # times than the per-IP login limit allows in fifteen minutes.
    from django.core.cache import cache

    cache.clear()
    return client.post(reverse("accounts:login"), {"username": user.email, "password": password})


class PasswordResetTests(TestCase):
    def setUp(self):
        self.user = create_user(email="priya@example.com")

    def test_sign_in_offers_a_reset_link(self):
        response = self.client.get(reverse("accounts:login"))

        self.assertContains(response, reverse("accounts:password_reset"))

    def test_a_reset_link_sets_a_new_password_once(self):
        response = self.client.post(
            reverse("accounts:password_reset"), {"email": "PRIYA@example.com"}
        )

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r"http://testserver(/accounts/password/reset/\S+/)", mail.outbox[0].body)
        self.assertIsNotNone(link)
        path = link.group(1)

        self.assertEqual(self.client.get(path).status_code, 200)
        response = self.client.post(path, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertTrue(AuditLog.objects.filter(verb="account.password_reset").exists())

        # Single use: the token is burned, and the changed password also
        # invalidates Django's token.
        self.assertEqual(self.client.get(path).status_code, 400)
        again = self.client.post(
            path, {"password1": "Another-Pass-9911", "password2": "Another-Pass-9911"}
        )
        self.assertEqual(again.status_code, 400)

    def test_an_unknown_address_gets_the_same_answer_and_no_email(self):
        known = self.client.post(reverse("accounts:password_reset"), {"email": "priya@example.com"})
        unknown = self.client.post(
            reverse("accounts:password_reset"), {"email": "nobody@example.com"}
        )

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known["Location"], unknown["Location"])
        self.assertEqual(len(mail.outbox), 1)

    def test_a_weak_or_mismatched_password_is_refused(self):
        found = identity.request_password_reset(email=self.user.email)
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        path = reverse(
            "accounts:password_reset_confirm",
            args=[urlsafe_base64_encode(force_bytes(self.user.pk)), found[1]],
        )
        self.assertEqual(
            self.client.post(path, {"password1": "short", "password2": "short"}).status_code, 400
        )
        self.assertEqual(
            self.client.post(
                path, {"password1": NEW_PASSWORD, "password2": "different"}
            ).status_code,
            400,
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_a_malformed_link_is_a_400_not_a_500(self):
        for uid in ("!!!", "Zm9v", "MTIzNDU2Nzg5"):
            with self.subTest(uid=uid):
                path = reverse("accounts:password_reset_confirm", args=[uid, "nope"])
                self.assertEqual(self.client.get(path).status_code, 400)
                self.assertEqual(
                    self.client.post(
                        path, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
                    ).status_code,
                    400,
                )

    def test_reset_requests_are_rate_limited(self):
        from apps.common import ratelimit

        with mock.patch.object(
            ratelimit,
            "enforce",
            side_effect=ratelimit.RateLimited(ratelimit.PASSWORD_RESET_PER_IDENTITY),
        ):
            response = self.client.post(
                reverse("accounts:password_reset"), {"email": "priya@example.com"}
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(mail.outbox), 0)


class SessionRevocationTests(TestCase):
    """A revoked session must stop working — including from the session cache."""

    def setUp(self):
        self.user = create_user()

    def test_a_revoked_session_is_signed_out_even_when_cached(self):
        other = self.client_class()
        _sign_in(other, self.user)
        self.assertEqual(other.get(reverse("settings:security")).status_code, 200)
        _sign_in(self.client, self.user)

        row = UserSession.objects.get(session_key=other.session.session_key)
        response = self.client.post(
            reverse("settings:security_act"), {"command": "revoke_session", "session": row.pk}
        )

        self.assertRedirects(response, reverse("settings:security"), fetch_redirect_response=False)
        after = other.get(reverse("settings:security"))
        self.assertEqual(after.status_code, 302)
        self.assertIn(reverse("accounts:login"), after["Location"])
        # This session survives.
        self.assertEqual(self.client.get(reverse("settings:security")).status_code, 200)

    def test_the_page_never_prints_a_session_key(self):
        other = self.client_class()
        _sign_in(other, self.user)
        _sign_in(self.client, self.user)

        body = self.client.get(reverse("settings:security")).content.decode()

        for key in UserSession.objects.filter(user=self.user).values_list("session_key", flat=True):
            self.assertNotIn(key, body)

    def test_another_persons_session_cannot_be_revoked(self):
        stranger = create_user()
        theirs = self.client_class()
        _sign_in(theirs, stranger)
        _sign_in(self.client, self.user)
        row = UserSession.objects.get(user=stranger)

        self.client.post(
            reverse("settings:security_act"), {"command": "revoke_session", "session": row.pk}
        )

        self.assertEqual(theirs.get(reverse("settings:security")).status_code, 200)

    def test_sign_out_everywhere_else(self):
        others = [self.client_class() for _ in range(2)]
        for other in others:
            _sign_in(other, self.user)
        _sign_in(self.client, self.user)

        self.client.post(reverse("settings:security_act"), {"command": "revoke_others"})

        for other in others:
            self.assertEqual(other.get(reverse("settings:security")).status_code, 302)
        self.assertEqual(self.client.get(reverse("settings:security")).status_code, 200)


class PasswordChangeTests(TestCase):
    def setUp(self):
        self.user = create_user()
        _sign_in(self.client, self.user)

    def test_the_account_page_links_to_somewhere_that_changes_it(self):
        response = self.client.get(reverse("settings:index"))

        self.assertContains(response, reverse("settings:security"))
        self.assertNotContains(response, 'name="email"')

    def test_changing_it_keeps_this_session_and_ends_the_others(self):
        other = self.client_class()
        _sign_in(other, self.user)

        response = self.client.post(
            reverse("settings:security_act"),
            {
                "command": "password",
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )

        self.assertRedirects(response, reverse("settings:security"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertEqual(self.client.get(reverse("settings:security")).status_code, 200)
        self.assertEqual(other.get(reverse("settings:security")).status_code, 302)
        self.assertTrue(AuditLog.objects.filter(verb="account.password_changed").exists())

    def test_a_wrong_current_password_changes_nothing(self):
        self.client.post(
            reverse("settings:security_act"),
            {
                "command": "password",
                "current_password": "wrong",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_an_unknown_command_is_refused_with_a_message(self):
        response = self.client.post(
            reverse("settings:security_act"), {"command": "delete_everything"}
        )

        self.assertRedirects(response, reverse("settings:security"), fetch_redirect_response=False)


class VerificationResendTests(TestCase):
    def test_an_unconfirmed_address_can_ask_for_a_new_link(self):
        user = create_user(email="new@example.com")
        EmailAddress.objects.create(user=user, email=user.email, is_primary=True)
        _sign_in(self.client, user)

        page = self.client.get(reverse("settings:security"))
        self.assertContains(page, reverse("accounts:verify_resend"))

        self.client.post(reverse("accounts:verify_resend"))

        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r"http://testserver(/accounts/verify/\S+/)", mail.outbox[0].body).group(1)
        self.client.get(link)
        self.assertIsNotNone(EmailAddress.objects.get(user=user).verified_at)

    def test_resend_requires_signing_in(self):
        response = self.client.post(reverse("accounts:verify_resend"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)


class TwoFactorTests(TestCase):
    def setUp(self):
        self.user = create_user()
        _sign_in(self.client, self.user)

    def enrol(self):
        self.client.post(reverse("settings:security_act"), {"command": "mfa_begin"})
        device = mfa.totp_device(self.user, confirmed=False)
        self.client.post(
            reverse("settings:security_act"), {"command": "mfa_confirm", "code": _code(device)}
        )
        return mfa.totp_device(self.user, confirmed=True)

    def test_setting_it_up_shows_recovery_codes_once(self):
        self.client.post(reverse("settings:security_act"), {"command": "mfa_begin"})
        pending = self.client.get(reverse("settings:security"))
        self.assertContains(pending, "Code from the app")

        device = mfa.totp_device(self.user, confirmed=False)
        self.client.post(
            reverse("settings:security_act"), {"command": "mfa_confirm", "code": _code(device)}
        )

        first = self.client.get(reverse("settings:security"))
        self.assertContains(first, "Your recovery codes")
        second = self.client.get(reverse("settings:security"))
        self.assertNotContains(second, "Your recovery codes")
        self.assertTrue(mfa.is_enabled(self.user))

    def test_a_wrong_setup_code_enables_nothing(self):
        self.client.post(reverse("settings:security_act"), {"command": "mfa_begin"})
        self.client.post(
            reverse("settings:security_act"), {"command": "mfa_confirm", "code": "000000"}
        )

        self.assertFalse(mfa.is_enabled(self.user))

    def test_sign_in_asks_for_the_code_and_nothing_is_signed_in_before_it(self):
        device = self.enrol()
        fresh = self.client_class()

        response = _sign_in(fresh, self.user)
        self.assertRedirects(
            response, reverse("accounts:mfa_challenge"), fetch_redirect_response=False
        )
        # Password right, code owed: not signed in yet.
        self.assertEqual(fresh.get(reverse("settings:security")).status_code, 302)

        wrong = fresh.post(reverse("accounts:mfa_challenge"), {"code": "000000"})
        self.assertEqual(wrong.status_code, 400)

        # The enrolment already spent this time step, and TOTP refuses a replay.
        # Clearing the marker stands in for the thirty seconds a person waits.
        # The wrong code also started django-otp's throttle, which is the point
        # of it; resetting stands in for the wait.
        device.refresh_from_db()
        device.last_t = -1
        device.throttle_reset(commit=False)
        device.save()
        right = fresh.post(reverse("accounts:mfa_challenge"), {"code": _code(device)})
        self.assertEqual(right.status_code, 302)
        self.assertEqual(fresh.get(reverse("settings:security")).status_code, 200)

    def test_a_recovery_code_signs_in_once(self):
        self.enrol()
        codes = mfa.regenerate_recovery_codes(user=self.user)
        fresh = self.client_class()
        _sign_in(fresh, self.user)

        self.assertEqual(
            fresh.post(reverse("accounts:mfa_challenge"), {"code": codes[0]}).status_code, 302
        )

        again = self.client_class()
        _sign_in(again, self.user)
        self.assertEqual(
            again.post(reverse("accounts:mfa_challenge"), {"code": codes[0]}).status_code, 400
        )

    def test_the_challenge_expires(self):
        self.enrol()
        codes = mfa.regenerate_recovery_codes(user=self.user)
        fresh = self.client_class()
        _sign_in(fresh, self.user)
        session = fresh.session
        pending = session["accounts:mfa_pending"]
        pending["at"] = pending["at"] - 3600
        session["accounts:mfa_pending"] = pending
        session.save()

        response = fresh.post(reverse("accounts:mfa_challenge"), {"code": codes[0]})

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)

    def test_the_challenge_does_not_open_a_redirect_elsewhere(self):
        self.enrol()
        codes = mfa.regenerate_recovery_codes(user=self.user)
        fresh = self.client_class()
        fresh.post(
            reverse("accounts:login") + "?next=https://evil.example/steal",
            {
                "username": self.user.email,
                "password": PASSWORD,
                "next": "https://evil.example/steal",
            },
        )

        response = fresh.post(reverse("accounts:mfa_challenge"), {"code": codes[0]})

        self.assertNotIn("evil.example", response["Location"])


@override_settings(ROOT_URLCONF="config.urls")
class TwoFactorRequirementTests(TestCase):
    """Mandatory for platform staff and company owners/admins (§2.1, FR-1.5)."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", quiet_summary=True)

    def test_staff_inside_the_grace_period_are_let_through(self):
        user = create_user()
        StaffRole.objects.create(user=user, level="support")
        self.client.force_login(user)

        self.assertEqual(self.client.get(reverse("staff:operations")).status_code, 200)

    def test_staff_past_the_grace_period_are_sent_to_set_it_up(self):
        user = create_user()
        StaffRole.objects.create(
            user=user, level="support", granted_at=timezone.now() - dt.timedelta(days=15)
        )
        self.client.force_login(user)

        response = self.client.get(reverse("staff:operations"))

        self.assertRedirects(response, reverse("settings:security"), fetch_redirect_response=False)
        # The page that satisfies the requirement is never itself blocked.
        self.assertEqual(self.client.get(reverse("settings:security")).status_code, 200)

    def test_staff_with_a_device_but_an_unchallenged_session_must_enter_a_code(self):
        user = create_user()
        StaffRole.objects.create(user=user, level="support")
        TOTPDevice.objects.create(user=user, name=mfa.TOTP_DEVICE_NAME, confirmed=True)
        self.client.force_login(user)

        response = self.client.get(reverse("staff:operations"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:mfa_challenge"), response["Location"])
        codes = mfa.regenerate_recovery_codes(user=user)
        self.client.post(response["Location"], {"code": codes[0]})
        self.assertEqual(self.client.get(reverse("staff:operations")).status_code, 200)

    def test_an_overdue_company_owner_is_stopped_but_a_recruiter_is_not(self):
        from apps.candidates.tests.factories import create_profile
        from apps.companies.models import CompanyMembership
        from apps.companies.permissions import Role
        from apps.companies.tests.factories import company_with_owner

        owner = create_profile().user
        company, membership = company_with_owner(owner)
        CompanyMembership.objects.filter(pk=membership.pk).update(
            created_at=timezone.now() - dt.timedelta(days=20)
        )
        recruiter = create_profile().user
        CompanyMembership.objects.create(company=company, user=recruiter, role=Role.RECRUITER)

        self.client.force_login(owner)
        blocked = self.client.get(reverse("employer:overview", args=[company.slug]))
        self.assertRedirects(blocked, reverse("settings:security"), fetch_redirect_response=False)

        self.client.force_login(recruiter)
        self.assertEqual(
            self.client.get(reverse("employer:overview", args=[company.slug])).status_code, 200
        )

    def test_a_required_role_cannot_turn_it_off(self):
        user = create_user()
        StaffRole.objects.create(user=user, level="support")
        self.client.force_login(user)
        TOTPDevice.objects.create(user=user, name=mfa.TOTP_DEVICE_NAME, confirmed=True)
        codes = mfa.regenerate_recovery_codes(user=user)

        self.client.post(
            reverse("settings:security_act"), {"command": "mfa_disable", "code": codes[0]}
        )

        self.assertTrue(mfa.is_enabled(user))


class ExpiredSessionListTests(TestCase):
    def test_a_session_that_has_expired_is_not_listed_as_signed_in(self):
        from django.contrib.sessions.models import Session

        user = create_user()
        old = self.client_class()
        _sign_in(old, user)
        Session.objects.filter(session_key=old.session.session_key).update(
            expire_date=timezone.now() - dt.timedelta(days=1)
        )
        _sign_in(self.client, user)

        rows = list(identity.active_sessions(user))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_key, self.client.session.session_key)
