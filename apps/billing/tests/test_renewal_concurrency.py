"""
Two nightly renewal runs at the same moment (Loop Closure BIL-01).

The Billing report left this untested: invoice creation is constraint-backed,
but the renewal itself took no lock. Celery delivers a scheduled task at least
once, so two `renew_due` runs overlapping is a normal event, not a fault.
`TransactionTestCase` because the defence is a row lock; the window is widened
the way `apps/companies/tests/test_team_concurrency.py` widens its own.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from unittest import mock

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

from apps.billing import tasks
from apps.billing.models import Invoice, Subscription
from apps.billing.services import subscriptions
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.notifications.models import OutboxEvent


class ConcurrentRenewalTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        factories.seed()
        self.subscription = factories.put_on_pro(create_profile())
        today = timezone.localdate()
        Subscription.objects.filter(pk=self.subscription.pk).update(
            current_period_start=today - dt.timedelta(days=31),
            current_period_end=today - dt.timedelta(days=1),
        )
        self.subscription.refresh_from_db()
        self.old_end = self.subscription.current_period_end
        self.day = today.isoformat()

    def test_two_overlapping_runs_renew_once_bill_once_and_announce_once(self):
        real_renew = subscriptions.renew

        def slow_renew(**kwargs):
            time.sleep(0.4)  # both runs have now read the subscription as due
            return real_renew(**kwargs)

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def run():
            try:
                barrier.wait(timeout=5)
                tasks.renew_due(self.day)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)
            finally:
                connections.close_all()

        with mock.patch.object(subscriptions, "renew", side_effect=slow_renew):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.current_period_start, self.old_end)
        self.assertEqual(
            Invoice.objects.filter(
                subscription=self.subscription, period_start=self.old_end
            ).count(),
            1,
        )
        self.assertEqual(
            OutboxEvent.objects.filter(type="subscription.renewed").count(),
            1,
            "both runs renewed the same period — every subscriber heard it twice",
        )
