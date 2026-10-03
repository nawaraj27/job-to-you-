"""
Two weekly digest sends for one person at the same instant (Loop Closure NOT-02).

The Notifications report added a row lock and a "sent this week" check, and
tested sequential repeats only. This drives the two at once with a widened
window, the way `apps/companies/tests/test_team_concurrency.py` does.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

from django.db import connections
from django.test import TransactionTestCase

from apps.notifications.models import EmailMessage
from apps.notifications.services import digests
from apps.notifications.tests.test_delivery_loop_regressions import reader


class ConcurrentDigestTests(TransactionTestCase):
    reset_sequences = True

    def test_two_simultaneous_sends_deliver_one_digest(self):
        user = reader()
        real_compose = digests._compose

        def slow_compose(items):
            time.sleep(0.4)  # the first send holds the lock with nothing written yet
            return real_compose(items)

        results: list[bool] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def send():
            try:
                barrier.wait(timeout=5)
                results.append(digests.send_to(user=user))
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)
            finally:
                connections.close_all()

        with mock.patch.object(digests, "_compose", side_effect=slow_compose):
            threads = [threading.Thread(target=send) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(
            EmailMessage.objects.filter(user=user, template_key="digest.weekly").count(), 1
        )
