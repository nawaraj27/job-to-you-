"""
Field-level encryption (DEVELOPER.md §6.11).

Rotation is the part worth testing hardest. Everything else about an encrypted
field is visible the first time you use it; a rotation that drops the old key
too early is invisible until someone opens a row written last month.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from django.core.checks import Error
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from apps.common import crypto
from apps.common.checks import check_field_encryption_keys

SECOND_KEY = Fernet.generate_key().decode()


class RoundTripTests(SimpleTestCase):
    def setUp(self):
        crypto.reset_cipher_cache()
        self.addCleanup(crypto.reset_cipher_cache)

    def test_a_value_survives_the_round_trip(self):
        self.assertEqual(crypto.decrypt(crypto.encrypt("+977 9812345678")), "+977 9812345678")

    def test_the_ciphertext_does_not_contain_the_plaintext(self):
        self.assertNotIn("9812345678", crypto.encrypt("+977 9812345678"))

    def test_two_encryptions_of_one_value_differ(self):
        # Randomised output is why encrypted columns support no equality
        # lookup and no index. Stated as a test so nobody adds one.
        self.assertNotEqual(crypto.encrypt("same"), crypto.encrypt("same"))

    def test_unicode_survives(self):
        self.assertEqual(crypto.decrypt(crypto.encrypt("ने ९८१२")), "ने ९८१२")

    def test_no_key_is_a_configuration_error_not_a_silent_pass_through(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=[]):
            crypto.reset_cipher_cache()
            with self.assertRaises(ImproperlyConfigured):
                crypto.encrypt("x")

    def test_a_key_that_is_not_a_fernet_key_is_a_configuration_error(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=["not-a-key"]):
            crypto.reset_cipher_cache()
            with self.assertRaises(ImproperlyConfigured):
                crypto.encrypt("x")


class RotationTests(TestCase):
    def setUp(self):
        crypto.reset_cipher_cache()
        self.addCleanup(crypto.reset_cipher_cache)

    def test_a_value_written_under_the_old_key_still_reads_after_rotation(self):
        from apps.accounts.tests.factories import create_user
        from apps.candidates.models import CandidateProfile

        original = crypto.encrypt("+977 9812345678")
        profile = CandidateProfile.objects.create(user=create_user(), phone="+977 9812345678")

        keys = [SECOND_KEY, *self._current_keys()]
        with override_settings(FIELD_ENCRYPTION_KEYS=keys):
            crypto.reset_cipher_cache()

            # Old ciphertext still decrypts...
            self.assertEqual(crypto.decrypt(original), "+977 9812345678")
            self.assertEqual(CandidateProfile.objects.get(pk=profile.pk).phone, "+977 9812345678")
            # ...and new writes use the new key.
            profile.phone = "+977 9800000000"
            profile.save(update_fields=["phone"])

        crypto.reset_cipher_cache()
        with self.assertRaises(ValueError):
            # Back on the old key alone, the newly written row is unreadable —
            # which is exactly why the old key is dropped only after re-saving.
            _unreadable = CandidateProfile.objects.get(pk=profile.pk).phone

    def _current_keys(self) -> list[str]:
        from django.conf import settings

        return list(settings.FIELD_ENCRYPTION_KEYS)


class DeployCheckTests(SimpleTestCase):
    def test_the_check_passes_with_a_valid_key(self):
        self.assertEqual(check_field_encryption_keys(None), [])

    def test_the_check_fails_with_no_key(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=[]):
            findings = check_field_encryption_keys(None)

        self.assertEqual(findings[0].id, "common.E001")
        self.assertIsInstance(findings[0], Error)

    def test_the_check_fails_with_a_malformed_key(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=["nope"]):
            findings = check_field_encryption_keys(None)

        self.assertEqual(findings[0].id, "common.E002")
