"""Idempotency claims (DEVELOPER.md §3.8)."""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import IdempotencyKey
from apps.accounts.tests.factories import create_user
from apps.common import idempotency
from apps.common.errors import Conflict

ENDPOINT = "applications.submit"


class IdempotencyTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.request_hash = idempotency.fingerprint({"job": 1, "resume": 2})

    def _claim(self, key="k-1", request_hash=None):
        return idempotency.claim(
            key=key,
            endpoint=ENDPOINT,
            request_hash=request_hash or self.request_hash,
            user=self.user,
        )

    def test_first_claim_is_not_a_replay(self):
        claim = self._claim()
        self.assertFalse(claim.replayed)
        self.assertIsNone(claim.response)
        self.assertTrue(IdempotencyKey.objects.filter(key="k-1").exists())

    def test_replay_after_completion_returns_the_stored_outcome(self):
        self._claim()
        idempotency.record(key="k-1", endpoint=ENDPOINT, response={"application_id": 7})

        claim = self._claim()

        self.assertTrue(claim.replayed)
        self.assertEqual(claim.response, {"application_id": 7})

    def test_replay_while_in_flight_conflicts(self):
        # The first attempt claimed the key and has not finished. Proceeding
        # would create a second record, which is the whole failure being
        # prevented, so the caller is told to retry rather than allowed through.
        self._claim()
        with self.assertRaises(Conflict) as caught:
            self._claim()
        self.assertEqual(caught.exception.code, "idempotency_in_flight")

    def test_same_key_with_a_different_body_is_rejected(self):
        self._claim()
        with self.assertRaises(Conflict) as caught:
            self._claim(request_hash=idempotency.fingerprint({"job": 99}))
        self.assertEqual(caught.exception.code, "idempotency_key_reused")

    def test_expired_claim_is_replaced_rather_than_replayed(self):
        self._claim()
        IdempotencyKey.objects.filter(key="k-1").update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        claim = self._claim()

        self.assertFalse(claim.replayed)
        self.assertEqual(IdempotencyKey.objects.filter(key="k-1").count(), 1)

    def test_the_same_key_on_two_endpoints_does_not_collide(self):
        self._claim()
        other = idempotency.claim(
            key="k-1",
            endpoint="messaging.send",
            request_hash=self.request_hash,
            user=self.user,
        )
        self.assertFalse(other.replayed)

    def test_release_frees_a_claim_that_recorded_no_outcome(self):
        self._claim()
        idempotency.release(key="k-1", endpoint=ENDPOINT)
        self.assertFalse(self._claim().replayed)

    def test_release_refuses_to_free_a_completed_claim(self):
        # Freeing a completed claim would let a retry duplicate the record.
        self._claim()
        idempotency.record(key="k-1", endpoint=ENDPOINT, response={"ok": True})
        idempotency.release(key="k-1", endpoint=ENDPOINT)
        self.assertTrue(self._claim().replayed)

    def test_purge_removes_only_expired_keys(self):
        self._claim(key="live")
        self._claim(key="dead")
        IdempotencyKey.objects.filter(key="dead").update(
            expires_at=timezone.now() - timedelta(hours=1)
        )

        self.assertEqual(idempotency.purge_expired(), 1)
        self.assertTrue(IdempotencyKey.objects.filter(key="live").exists())
