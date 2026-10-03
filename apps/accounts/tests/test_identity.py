"""Registration, verification, password reset, sessions, consent and MFA."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Consent, EmailAddress, UserSession
from apps.accounts.services import identity, mfa
from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.common.errors import Conflict, InvalidState, NotAllowed
from apps.notifications.models import OutboxEvent


class RegistrationTests(TestCase):
    def test_registering_creates_an_unverified_primary_address(self):
        user = identity.register(email="New@Example.com", password="s3cret-pass-phrase")

        address = EmailAddress.objects.get(user=user)
        self.assertEqual(address.email, "new@example.com")
        self.assertTrue(address.is_primary)
        self.assertFalse(address.is_verified)

    def test_registration_and_verification_are_separate_events(self):
        # Onboarding subscribes to registration; anything privileged waits for
        # verification. Collapsing them would let one subscriber do both jobs.
        identity.register(email="new@example.com", password="s3cret-pass-phrase")

        self.assertTrue(OutboxEvent.objects.filter(type="user.registered").exists())
        self.assertFalse(OutboxEvent.objects.filter(type="account.verified").exists())

    def test_duplicate_registration_is_refused(self):
        identity.register(email="taken@example.com", password="s3cret-pass-phrase")

        with self.assertRaises(Conflict):
            identity.register(email="TAKEN@example.com", password="another-pass-phrase")

    def test_registration_is_audited(self):
        identity.register(email="new@example.com", password="s3cret-pass-phrase")
        self.assertTrue(AuditLog.objects.filter(verb="user.registered").exists())


class EmailVerificationTests(TestCase):
    def setUp(self):
        self.user = identity.register(email="new@example.com", password="s3cret-pass-phrase")
        self.address = EmailAddress.objects.get(user=self.user)

    def test_a_valid_token_confirms_the_address(self):
        token = identity.issue_verification_token(address=self.address)

        identity.verify_email(token=token)

        self.address.refresh_from_db()
        self.assertTrue(self.address.is_verified)
        self.assertTrue(OutboxEvent.objects.filter(type="account.verified").exists())

    def test_a_verification_token_works_exactly_once(self):
        token = identity.issue_verification_token(address=self.address)
        identity.verify_email(token=token)

        with self.assertRaises(NotAllowed):
            identity.verify_email(token=token)

    def test_a_tampered_token_is_refused(self):
        token = identity.issue_verification_token(address=self.address)

        with self.assertRaises(NotAllowed):
            identity.verify_email(token=token[:-2] + "xy")


class PasswordResetTests(TestCase):
    def setUp(self):
        self.user = create_user(email="person@example.com")

    def test_an_unknown_address_returns_none_rather_than_raising(self):
        # The reset form must not become an account-enumeration oracle, so the
        # caller shows one message either way.
        self.assertIsNone(identity.request_password_reset(email="nobody@example.com"))

    def test_reset_sets_the_new_password(self):
        _, token = identity.request_password_reset(email="person@example.com")

        identity.reset_password(user=self.user, token=token, new_password="a-new-pass-phrase")

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("a-new-pass-phrase"))

    def test_a_reset_token_cannot_be_replayed(self):
        _, token = identity.request_password_reset(email="person@example.com")
        identity.reset_password(user=self.user, token=token, new_password="a-new-pass-phrase")

        with self.assertRaises(NotAllowed):
            identity.reset_password(user=self.user, token=token, new_password="third-pass-phrase")

    def test_an_invalid_token_is_refused(self):
        with self.assertRaises(NotAllowed):
            identity.reset_password(
                user=self.user, token="not-a-token", new_password="whatever-pass"
            )

    def test_reset_revokes_every_existing_session(self):
        # A password reset after a compromise is worthless if the attacker's
        # session survives it.
        identity.record_session(
            user=self.user, session_key="abc123", ip="127.0.0.1", user_agent="test"
        )
        _, token = identity.request_password_reset(email="person@example.com")

        identity.reset_password(user=self.user, token=token, new_password="a-new-pass-phrase")

        self.assertFalse(
            UserSession.objects.filter(user=self.user, revoked_at__isnull=True).exists()
        )


class SessionTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.other = create_user()

    def test_a_user_cannot_revoke_someone_else_s_session(self):
        identity.record_session(user=self.other, session_key="theirs", ip=None, user_agent="test")

        with self.assertRaises(NotAllowed):
            identity.revoke_session(actor=self.user, session_key="theirs")

    def test_revoke_all_can_keep_the_current_session(self):
        identity.record_session(user=self.user, session_key="current", ip=None, user_agent="t")
        identity.record_session(user=self.user, session_key="other", ip=None, user_agent="t")

        revoked = identity.revoke_all_sessions(user=self.user, except_key="current")

        self.assertEqual(revoked, 1)
        self.assertTrue(
            UserSession.objects.filter(session_key="current", revoked_at__isnull=True).exists()
        )


class ConsentTests(TestCase):
    def setUp(self):
        self.user = create_user()

    def test_consent_defaults_to_absent(self):
        self.assertFalse(
            has := identity.has_consent(user=self.user, purpose=Consent.Purpose.AI_TRAINING)
        )
        self.assertFalse(has)

    def test_granting_and_withdrawing_appends_rather_than_updates(self):
        # The history of what someone agreed to must survive them changing
        # their mind, or an audit can only see the latest answer.
        identity.record_consent(
            user=self.user,
            purpose=Consent.Purpose.AI_TRAINING,
            granted=True,
            policy_version="2026-08",
        )
        identity.record_consent(
            user=self.user,
            purpose=Consent.Purpose.AI_TRAINING,
            granted=False,
            policy_version="2026-08",
        )

        self.assertEqual(Consent.objects.filter(user=self.user).count(), 2)
        self.assertFalse(identity.has_consent(user=self.user, purpose=Consent.Purpose.AI_TRAINING))

    def test_current_state_is_the_newest_decision(self):
        identity.record_consent(
            user=self.user, purpose=Consent.Purpose.MARKETING, granted=False, policy_version="1"
        )
        identity.record_consent(
            user=self.user, purpose=Consent.Purpose.MARKETING, granted=True, policy_version="1"
        )

        self.assertTrue(identity.has_consent(user=self.user, purpose=Consent.Purpose.MARKETING))

    def test_an_unknown_purpose_is_refused(self):
        with self.assertRaises(InvalidState):
            identity.record_consent(
                user=self.user, purpose="mind_reading", granted=True, policy_version="1"
            )


class MFATests(TestCase):
    """django-otp integration (ADR-A42)."""

    def setUp(self):
        self.user = create_user()

    def _confirm(self) -> list[str]:
        device = mfa.begin_enrolment(user=self.user)
        return mfa.confirm_enrolment(user=self.user, code=_totp(device))

    def test_a_new_account_has_no_second_factor(self):
        self.assertFalse(mfa.is_enabled(self.user))

    def test_enrolment_creates_an_unconfirmed_device(self):
        # Unusable until confirmed, so an abandoned setup cannot lock anyone out.
        device = mfa.begin_enrolment(user=self.user)

        self.assertFalse(device.confirmed)
        self.assertFalse(mfa.is_enabled(self.user))

    def test_a_wrong_code_does_not_confirm(self):
        mfa.begin_enrolment(user=self.user)

        with self.assertRaises(NotAllowed):
            mfa.confirm_enrolment(user=self.user, code="000000")

        self.assertFalse(mfa.is_enabled(self.user))

    def test_confirming_enables_mfa_and_issues_recovery_codes(self):
        codes = self._confirm()

        self.assertTrue(mfa.is_enabled(self.user))
        self.assertEqual(len(codes), mfa.RECOVERY_CODE_COUNT)
        self.assertEqual(mfa.remaining_recovery_codes(user=self.user), mfa.RECOVERY_CODE_COUNT)

    def test_a_recovery_code_works_once(self):
        codes = self._confirm()

        self.assertTrue(mfa.verify(user=self.user, code=codes[0]))
        self.assertFalse(mfa.verify(user=self.user, code=codes[0]))
        self.assertEqual(mfa.remaining_recovery_codes(user=self.user), mfa.RECOVERY_CODE_COUNT - 1)

    def test_restarting_enrolment_discards_the_previous_secret(self):
        first = mfa.begin_enrolment(user=self.user)
        second = mfa.begin_enrolment(user=self.user)

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(TOTPDevice.objects.filter(user=self.user).count(), 1)

    def test_enrolling_twice_is_refused_once_enabled(self):
        self._confirm()

        with self.assertRaises(Conflict):
            mfa.begin_enrolment(user=self.user)

    def test_disabling_requires_a_valid_code(self):
        # A stolen session must not be able to quietly remove the second factor.
        self._confirm()

        with self.assertRaises(NotAllowed):
            mfa.disable(user=self.user, code="000000")

        self.assertTrue(mfa.is_enabled(self.user))

    def test_disabling_with_a_recovery_code_removes_both_devices(self):
        codes = self._confirm()

        mfa.disable(user=self.user, code=codes[0])

        self.assertFalse(mfa.is_enabled(self.user))
        self.assertEqual(mfa.remaining_recovery_codes(user=self.user), 0)

    def test_regenerating_replaces_the_whole_set(self):
        first = self._confirm()
        second = mfa.regenerate_recovery_codes(user=self.user)

        self.assertEqual(len(second), mfa.RECOVERY_CODE_COUNT)
        self.assertFalse(mfa.verify(user=self.user, code=first[0]))

    def test_the_provisioning_uri_is_an_otpauth_url(self):
        device = mfa.begin_enrolment(user=self.user)
        self.assertTrue(mfa.provisioning_uri(device).startswith("otpauth://totp/"))


def _totp(device: TOTPDevice) -> str:
    """A currently valid code for a device, computed the way django-otp does."""
    from django_otp.oath import TOTP

    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return format(totp.token(), f"0{device.digits}d")


class LandingTests(TestCase):
    """Where signing up and signing in actually put somebody.

    Both landed on the marketing homepage — the page that sells the product to
    a person who has just bought it. A new candidate had to find the dashboard
    themselves, and the dashboard is the screen that tells them what to do
    first.
    """

    def _signup(self, email: str, intent: str):
        """Sign up, confirm from the email, sign in (decision D-12: sign-up
        itself signs nobody in, so the landing is the first sign-in's)."""
        import re

        from django.core import mail

        self.client.get(reverse("accounts:signup"))
        self.client.post(
            reverse("accounts:signup"),
            {
                "full_name": "Kiran Thapa",
                "email": email,
                "password1": "s3cret-pass-phrase",
                "password2": "s3cret-pass-phrase",
                "signup_intent": intent,
            },
        )
        link = re.search(r"http://testserver(/accounts/verify/\S+)", mail.outbox[-1].body)
        self.client.get(link.group(1))
        return self.client.post(
            reverse("accounts:login"), {"username": email, "password": "s3cret-pass-phrase"}
        )

    def test_a_new_candidate_lands_on_their_dashboard(self):
        response = self._signup("new-candidate@example.com", "candidate")

        self.assertRedirects(response, reverse("dashboard:index"), fetch_redirect_response=False)

    def test_somebody_here_to_hire_lands_in_the_employer_portal(self):
        response = self._signup("new-employer@example.com", "employer")

        self.assertRedirects(response, reverse("employer:index"), fetch_redirect_response=False)

    def test_signing_in_goes_to_the_same_home(self):
        user = create_user(email="returning@example.com", password="s3cret-pass-phrase")
        self.client.get(reverse("accounts:login"))

        response = self.client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "s3cret-pass-phrase"},
        )

        self.assertRedirects(response, reverse("dashboard:index"), fetch_redirect_response=False)

    def test_a_recruiter_signs_in_to_their_company(self):
        from apps.companies.tests.factories import company_with_owner

        user = create_user(email="recruiter@example.com", password="s3cret-pass-phrase")
        company_with_owner(user)
        self.client.get(reverse("accounts:login"))

        response = self.client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "s3cret-pass-phrase"},
        )

        self.assertRedirects(response, reverse("employer:index"), fetch_redirect_response=False)

    def test_where_they_were_going_still_wins(self):
        user = create_user(email="deeplink@example.com", password="s3cret-pass-phrase")
        target = reverse("jobs:saved")
        self.client.get(f"{reverse('accounts:login')}?next={target}")

        response = self.client.post(
            f"{reverse('accounts:login')}?next={target}",
            {"username": user.email, "password": "s3cret-pass-phrase"},
        )

        self.assertRedirects(response, target, fetch_redirect_response=False)
