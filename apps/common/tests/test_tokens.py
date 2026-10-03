"""
Single-use token durability (ADR-A38).

The point of moving consumption out of the cache: the guarantee must survive
eviction, `cache.clear()` and a Redis restart. These tests assert that
directly, because the previous implementation would have passed a naive
"token cannot be replayed" test while still being replayable in production.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.common import tokens
from apps.common.models import ConsumedToken


def _expiry(seconds: int = 300):
    return timezone.now() + timedelta(seconds=seconds)


class ConsumeTests(TestCase):
    def test_first_use_succeeds(self):
        self.assertTrue(
            tokens.consume(jti="t1", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())
        )

    def test_replay_is_refused(self):
        tokens.consume(jti="t1", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())
        self.assertFalse(
            tokens.consume(jti="t1", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())
        )

    def test_clearing_the_cache_cannot_re_arm_a_token(self):
        # The whole reason this ledger exists. Under the previous design this
        # assertion failed and the token became replayable.
        tokens.consume(jti="t1", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())

        cache.clear()

        self.assertFalse(
            tokens.consume(jti="t1", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())
        )

    def test_the_same_identifier_under_two_purposes_does_not_collide(self):
        self.assertTrue(
            tokens.consume(jti="t1", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())
        )
        self.assertTrue(
            tokens.consume(jti="t1", purpose=tokens.Purpose.PASSWORD_RESET, expires_at=_expiry())
        )

    def test_consumption_failure_does_not_poison_the_caller_s_transaction(self):
        # The insert runs in a savepoint, so a refused replay leaves the
        # surrounding transaction usable — the caller must be able to go on and
        # return a refusal rather than aborting.
        tokens.consume(jti="t1", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())
        tokens.consume(jti="t1", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())

        self.assertEqual(ConsumedToken.objects.count(), 1)

    def test_purge_removes_only_expired_records(self):
        tokens.consume(jti="live", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry())
        tokens.consume(jti="dead", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry(-10))

        self.assertEqual(tokens.purge_expired(), 1)
        self.assertTrue(ConsumedToken.objects.filter(jti="live").exists())


class ConcurrentConsumeTests(TransactionTestCase):
    """Real commit boundaries, so the unique constraint is genuinely exercised."""

    def test_only_one_of_two_racing_consumers_wins(self):
        import threading

        results: list[bool] = []
        barrier = threading.Barrier(2)

        def attempt():
            from django.db import connection

            barrier.wait()
            try:
                results.append(
                    tokens.consume(
                        jti="race", purpose=tokens.Purpose.FILE_ACCESS, expires_at=_expiry()
                    )
                )
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(ConsumedToken.objects.filter(jti="race").count(), 1)
