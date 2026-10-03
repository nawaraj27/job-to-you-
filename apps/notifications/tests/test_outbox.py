"""
The outbox guarantee (ADR-A14, ADR-A25).

Two properties are the whole point and are tested first: an event written in a
transaction that rolls back is never delivered, and an event written in a
transaction that commits is delivered exactly once however many times the drain
runs.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.notifications import consumers, events
from apps.notifications.models import OutboxConsumerRun, OutboxEvent
from apps.notifications.services import outbox

TEST_EVENT = "widget.tested"


def _register_test_event():
    # Registering the same spec twice is a no-op; a conflicting spec raises,
    # which would mean a real bug rather than a repeated setUp.
    with contextlib.suppress(ValueError):
        events.register(TEST_EVENT, schema_version=1, required={"widget_id"})


class _Recorder:
    """A consumer that counts its calls and can be told to fail."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.__module__ = "tests"
        self.__qualname__ = f"Recorder{id(self)}"

    def __call__(self, event: OutboxEvent) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("consumer exploded")


class OutboxDeliveryTests(TestCase):
    def setUp(self):
        _register_test_event()
        self.recorder = _Recorder()
        consumers._SUBSCRIBERS[TEST_EVENT] = [self.recorder]
        self.addCleanup(consumers._SUBSCRIBERS.pop, TEST_EVENT, None)

    def _emit(self):
        return events.emit(TEST_EVENT, payload={"widget_id": 1})

    def test_event_is_delivered_once(self):
        self._emit()
        self.assertEqual(outbox.drain(), 1)
        self.assertEqual(self.recorder.calls, 1)
        self.assertIsNotNone(OutboxEvent.objects.get().processed_at)

    def test_draining_twice_does_not_deliver_twice(self):
        self._emit()
        outbox.drain()
        outbox.drain()
        self.assertEqual(self.recorder.calls, 1)

    def test_ledger_records_one_row_per_consumer(self):
        self._emit()
        outbox.drain()

        # One row per consumer that ran, whichever they are. Asserted against
        # the registry rather than a literal, because wildcard subscribers are
        # added by later phases and a hard-coded count turns every one of them
        # into a false failure here.
        expected = len(consumers.consumers_for(TEST_EVENT))
        self.assertEqual(OutboxConsumerRun.objects.filter(status="ok").count(), expected)

    def test_a_failing_consumer_leaves_the_event_for_retry(self):
        self.recorder.fail_times = 1
        self._emit()

        self.assertEqual(outbox.drain(), 0)

        event = OutboxEvent.objects.get()
        self.assertIsNone(event.processed_at)
        self.assertEqual(event.attempts, 1)
        self.assertIn("consumer exploded", event.last_error)
        self.assertGreater(event.available_at, timezone.now())

    def test_retry_reruns_only_the_consumer_that_failed(self):
        # The logging consumer already succeeded; re-running it would duplicate
        # work that may not be idempotent in a later phase.
        self.recorder.fail_times = 1
        self._emit()
        outbox.drain()

        OutboxEvent.objects.update(available_at=timezone.now())
        self.assertEqual(outbox.drain(), 1)
        self.assertEqual(self.recorder.calls, 2)

        expected = len(consumers.consumers_for(TEST_EVENT))
        self.assertEqual(OutboxConsumerRun.objects.filter(status="ok").count(), expected)

    def test_a_failing_consumer_does_not_stop_the_others(self):
        other = _Recorder()
        consumers._SUBSCRIBERS[TEST_EVENT] = [_Recorder(fail_times=99), other]
        self._emit()

        outbox.drain()

        self.assertEqual(other.calls, 1)

    def test_event_is_dead_lettered_after_max_attempts(self):
        self.recorder.fail_times = 99
        self._emit()

        with self.settings(OUTBOX_MAX_ATTEMPTS=2):
            outbox.drain()
            OutboxEvent.objects.update(available_at=timezone.now())
            outbox.drain()

        event = OutboxEvent.objects.get()
        self.assertIsNotNone(event.dead_lettered_at)
        self.assertEqual(OutboxEvent.objects.pending().count(), 0)

    def test_events_scheduled_in_the_future_are_not_drained(self):
        self._emit()
        OutboxEvent.objects.update(available_at=timezone.now() + timedelta(minutes=5))
        self.assertEqual(outbox.drain(), 0)
        self.assertEqual(self.recorder.calls, 0)

    def test_purge_removes_delivered_events_only(self):
        self._emit()
        outbox.drain()
        OutboxEvent.objects.update(processed_at=timezone.now() - timedelta(hours=48))
        self._emit()

        self.assertEqual(outbox.purge_processed(older_than_hours=24), 1)
        self.assertEqual(OutboxEvent.objects.count(), 1)


class OutboxTransactionTests(TransactionTestCase):
    """Needs a real commit boundary, which TestCase's wrapping atomic hides."""

    def setUp(self):
        _register_test_event()
        self.recorder = _Recorder()
        consumers._SUBSCRIBERS[TEST_EVENT] = [self.recorder]
        self.addCleanup(consumers._SUBSCRIBERS.pop, TEST_EVENT, None)

    def test_rolled_back_transaction_leaves_no_event(self):
        # The guarantee: the world is never told about a change that did not
        # happen.
        with self.assertRaises(RuntimeError), transaction.atomic():
            events.emit(TEST_EVENT, payload={"widget_id": 1})
            raise RuntimeError("business rule failed after emitting")

        self.assertEqual(OutboxEvent.objects.count(), 0)
        outbox.drain()
        self.assertEqual(self.recorder.calls, 0)

    def test_committed_transaction_leaves_exactly_one_event(self):
        with transaction.atomic():
            events.emit(TEST_EVENT, payload={"widget_id": 1})

        self.assertEqual(OutboxEvent.objects.count(), 1)
        outbox.drain()
        self.assertEqual(self.recorder.calls, 1)

    def test_emit_outside_a_transaction_is_refused(self):
        # Outside a transaction there is nothing to tie the event to, so the
        # guarantee cannot hold and the call is a bug.
        with self.assertRaises(RuntimeError) as caught:
            events.emit(TEST_EVENT, payload={"widget_id": 1})
        self.assertIn("outside a transaction", str(caught.exception))
