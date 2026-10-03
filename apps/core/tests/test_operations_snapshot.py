"""
The operations board's numbers (SRS §17.4, OPS-01).

§17.4 names what an operator is alerted on — the oldest unprocessed outbox row,
queue depth, the AI error rate, AI daily spend — and `/healthz` and `/readyz`
covering the database, Redis and storage. This module is the selector that
board shares with those alerts, so the tests here are about the two ways a
number on an incident screen goes wrong:

  * it answers a different question than the one asked (yesterday's AI spend
    when somebody needs today's);
  * it cannot be produced at all, because the thing it measures is down — and
    the read raises instead of reporting, taking the board with it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.ai.models import AIRun, RunStatus
from apps.analytics.models import AICostDaily
from apps.core import selectors
from apps.core.models import FeatureFlag


def _run(*, status: str = RunStatus.OK, cost: str = "1.0000", task: str = "resume.analysis"):
    return AIRun.objects.create(
        task_key=task,
        provider="test",
        model="test-model",
        input_hash=f"{AIRun.objects.count():064d}",
        status=status,
        cost=cost,
    )


def _boom() -> None:
    raise RuntimeError("storage is gone")


class AiTodayTests(TestCase):
    """The question the board is opened to answer, about the day it is asked on."""

    def test_today_is_counted_from_the_runs_not_the_rollup(self):
        # The rollup is written at 01:10 for the day that has finished, so at
        # two in the afternoon it holds yesterday and nothing else. The board
        # showed only that, which made "what is AI costing" a question this
        # screen could not answer about today.
        AICostDaily.objects.create(
            day=timezone.localdate() - dt.timedelta(days=1),
            task_key="resume.analysis",
            runs=100,
            failures=0,
            cost="99.0000",
        )
        _run(cost="2.5000")
        _run(cost="2.5000", status=RunStatus.PROVIDER_ERROR)

        today = selectors.ai_today()

        self.assertEqual(today["runs"], 2)
        self.assertEqual(today["failures"], 1)
        self.assertEqual(today["cost"], Decimal("5.0000"))
        self.assertEqual(today["failure_rate"], 0.5)

    def test_a_run_from_yesterday_is_not_today(self):
        old = _run(cost="7.0000")
        AIRun.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - dt.timedelta(days=1, hours=2)
        )

        self.assertEqual(selectors.ai_today()["runs"], 0)
        self.assertEqual(selectors.ai_today()["cost"], Decimal("0"))

    def test_every_failure_status_counts_as_a_failure(self):
        # The same definition as the nightly rollup: anything that is not `ok`.
        # Two definitions of "failed" produce a board and a report that
        # disagree about the same afternoon.
        _run(status=RunStatus.OK)
        for status in (RunStatus.SCHEMA_ERROR, RunStatus.PROVIDER_ERROR, RunStatus.PENDING):
            _run(status=status)

        self.assertEqual(selectors.ai_today()["failures"], 3)

    def test_the_rows_are_per_task_most_expensive_first(self):
        _run(task="resume.analysis", cost="1.0000")
        _run(task="interview.evaluate", cost="9.0000")

        rows = selectors.ai_today()["rows"]

        self.assertEqual(
            [row["task_key"] for row in rows], ["interview.evaluate", "resume.analysis"]
        )

    def test_a_quiet_day_is_zero_rather_than_an_error(self):
        today = selectors.ai_today()

        self.assertEqual((today["runs"], today["failures"], today["failure_rate"]), (0, 0, 0.0))
        self.assertEqual(today["cost"], Decimal("0"))
        self.assertEqual(today["rows"], [])


class QueueDepthTests(TestCase):
    """§17.4 alerts on queue depth; the broker is the only place it exists."""

    def setUp(self):
        selectors._release_broker()
        self.addCleanup(selectors._release_broker)

    def test_every_declared_queue_is_reported(self):
        from config.celery import QUEUES

        channel = mock.Mock()
        channel._size.side_effect = lambda name: {"ai": 12, "email": 3}.get(name, 0)
        with mock.patch.object(selectors, "_broker_connection") as connect:
            connect.return_value.default_channel = channel
            result = selectors.queue_depths()

        self.assertTrue(result["available"])
        self.assertEqual(set(result["depths"]), set(QUEUES))
        self.assertEqual(result["total"], 15)

    def test_the_deepest_queue_is_first(self):
        channel = mock.Mock()
        channel._size.side_effect = lambda name: {"render": 40, "ai": 7}.get(name, 0)
        with mock.patch.object(selectors, "_broker_connection") as connect:
            connect.return_value.default_channel = channel
            depths = selectors.queue_depths()["depths"]

        self.assertEqual(list(depths)[:2], ["render", "ai"])

    def test_an_unreachable_broker_is_reported_not_raised(self):
        # The state the board exists to show. A read that raises here takes
        # down the one screen somebody opens when nothing is being worked.
        with mock.patch.object(
            selectors, "_broker_connection", side_effect=OSError("connection refused")
        ):
            result = selectors.queue_depths()

        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "OSError")
        self.assertEqual(result["depths"], {})

    def test_a_broken_connection_is_not_handed_to_the_next_reader(self):
        channel = mock.Mock()
        channel._size.side_effect = OSError("gone")
        with mock.patch.object(selectors, "_broker_connection") as connect:
            connect.return_value.default_channel = channel
            selectors.queue_depths()

        self.assertIsNone(selectors._broker)


class DependencyHealthTests(TestCase):
    def test_it_reports_the_same_three_probes_readyz_runs(self):
        from apps.common import health

        keys = [row["key"] for row in selectors.dependency_health()]

        self.assertEqual(keys, [probe.key for probe in health.PROBES])

    def test_a_healthy_platform_reads_ok(self):
        self.assertTrue(all(row["status"] == "ok" for row in selectors.dependency_health()))

    def test_a_failing_dependency_is_named_rather_than_raised(self):
        from apps.common import health

        with mock.patch.object(
            health, "PROBES", (health.Probe("private_storage", "Private storage", _boom),)
        ):
            rows = selectors.dependency_health()

        self.assertEqual(rows[0]["label"], "Private storage")
        self.assertEqual(rows[0]["status"], "error: RuntimeError")


class SnapshotTests(TestCase):
    def setUp(self):
        selectors._release_broker()
        self.addCleanup(selectors._release_broker)
        patcher = mock.patch.object(
            selectors, "queue_depths", return_value={"available": False, "depths": {}, "total": 0}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_snapshot_carries_every_question_the_board_asks(self):
        self.assertEqual(
            set(selectors.operations_snapshot()),
            {
                "outbox_pending",
                "outbox_oldest",
                "expired_flags",
                "ai_yesterday",
                "ai_today",
                "queues",
                "dependencies",
                "reference_data",
                "erasures_pending",
            },
        )

    def test_an_expired_flag_is_listed_and_a_live_one_is_not(self):
        FeatureFlag.objects.create(
            key="dead_branch",
            is_enabled=True,
            expires_on=timezone.localdate() - dt.timedelta(days=1),
        )
        FeatureFlag.objects.create(
            key="still_running",
            is_enabled=True,
            expires_on=timezone.localdate() + dt.timedelta(days=30),
        )

        self.assertEqual(selectors.operations_snapshot()["expired_flags"], ["dead_branch"])

    def test_the_outbox_reports_its_oldest_waiting_row(self):
        from apps.notifications.models import OutboxEvent

        older = OutboxEvent.objects.create(type="a", payload={})
        OutboxEvent.objects.create(type="b", payload={})
        OutboxEvent.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=3)
        )

        snapshot = selectors.operations_snapshot()

        self.assertEqual(snapshot["outbox_pending"], 2)
        self.assertEqual(snapshot["outbox_oldest"], OutboxEvent.objects.get(pk=older.pk).created_at)

    def test_a_processed_row_is_not_waiting(self):
        from apps.notifications.models import OutboxEvent

        OutboxEvent.objects.create(type="a", payload={}, processed_at=timezone.now())

        snapshot = selectors.operations_snapshot()

        self.assertEqual(snapshot["outbox_pending"], 0)
        self.assertIsNone(snapshot["outbox_oldest"])


class ReferenceDataTests(TestCase):
    def test_an_unseeded_database_names_each_missing_command(self):
        rows = {row["command"]: row for row in selectors.reference_data()}

        self.assertIn("seed_interview_templates", rows)
        self.assertFalse(rows["seed_interview_templates"]["ok"])

    def test_seeding_turns_the_row_green(self):
        from django.core.management import call_command

        call_command("seed_interview_templates", verbosity=0)

        rows = {row["command"]: row for row in selectors.reference_data()}
        self.assertTrue(rows["seed_interview_templates"]["ok"])
