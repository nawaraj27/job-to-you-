"""
Correlation context propagation (ADR-A37, SRS NFR-10).

NFR-10 previously claimed the request id reached Celery. It did not — a
ContextVar cannot cross a process boundary. These tests assert the propagation
that now makes the claim true, and the reset that stops one task's identifiers
leaking into the next.
"""

from __future__ import annotations

import json
import logging

from django.test import TestCase

from apps.common import logging as log
from apps.common.logging import JSONFormatter, LogContext, RequestIDFilter


class LogContextTests(TestCase):
    def tearDown(self):
        log.clear()

    def test_only_populated_fields_are_emitted(self):
        # An envelope of nulls is noise in every log line.
        context = LogContext(request_id="r1", actor_id=7)

        self.assertEqual(context.as_dict(), {"request_id": "r1", "actor_id": 7})

    def test_bind_adds_without_discarding_existing_fields(self):
        log.bind(request_id="r1")
        log.bind(actor_id=7)

        self.assertEqual(log.get_context().request_id, "r1")
        self.assertEqual(log.get_context().actor_id, 7)

    def test_reset_restores_the_previous_context(self):
        token = log.bind(request_id="r1")
        log.reset(token)

        self.assertIsNone(log.get_context().request_id)

    def test_the_formatter_emits_the_envelope(self):
        log.bind(request_id="r1", actor_id=7, company_id=3, task_id="t1", event_id=9)
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        RequestIDFilter().filter(record)

        payload = json.loads(JSONFormatter().format(record))

        for field, value in [
            ("request_id", "r1"),
            ("actor_id", 7),
            ("company_id", 3),
            ("task_id", "t1"),
            ("event_id", 9),
        ]:
            self.assertEqual(payload[field], value)

    def test_absent_identifiers_are_omitted_rather_than_null(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        RequestIDFilter().filter(record)

        payload = json.loads(JSONFormatter().format(record))

        self.assertNotIn("actor_id", payload)


class RequestContextMiddlewareTests(TestCase):
    def test_response_carries_a_request_id(self):
        self.assertTrue(self.client.get("/healthz")["X-Request-ID"])

    def test_inbound_request_id_is_honoured(self):
        response = self.client.get("/healthz", headers={"x-request-id": "abc-123"})
        self.assertEqual(response["X-Request-ID"], "abc-123")

    def test_request_id_with_a_newline_is_replaced(self):
        response = self.client.get("/healthz", headers={"x-request-id": "line\nbreak"})
        self.assertNotIn("\n", response["X-Request-ID"])

    def test_overlong_request_id_is_truncated(self):
        response = self.client.get("/healthz", headers={"x-request-id": "a" * 500})
        self.assertLessEqual(len(response["X-Request-ID"]), 64)

    def test_two_requests_get_different_ids(self):
        first = self.client.get("/healthz")["X-Request-ID"]
        second = self.client.get("/healthz")["X-Request-ID"]
        self.assertNotEqual(first, second)

    def test_context_does_not_leak_between_requests(self):
        self.client.get("/healthz")
        self.assertIsNone(log.get_context().request_id)

    def test_middleware_runs_before_authentication(self):
        # The ordering finding: with this middleware last, an authentication
        # failure logged with no id at all.
        from django.conf import settings

        order = settings.MIDDLEWARE
        self.assertLess(
            order.index("apps.common.middleware.RequestContextMiddleware"),
            order.index("django.contrib.auth.middleware.AuthenticationMiddleware"),
        )
        self.assertEqual(order[0], "apps.common.middleware.RequestContextMiddleware")


class CeleryPropagationTests(TestCase):
    def tearDown(self):
        log.clear()

    def test_publishing_a_task_attaches_the_envelope_to_its_headers(self):
        from config.celery import _attach_log_context

        log.bind(request_id="r1", actor_id=7)
        headers: dict = {}

        _attach_log_context(headers=headers)

        self.assertEqual(headers[log.HEADER_KEY], {"request_id": "r1", "actor_id": 7})

    def test_nothing_is_attached_when_there_is_no_context(self):
        from config.celery import _attach_log_context

        headers: dict = {}
        _attach_log_context(headers=headers)

        self.assertNotIn(log.HEADER_KEY, headers)

    def test_a_running_task_restores_the_envelope_and_gains_a_task_id(self):
        from config.celery import _restore_log_context

        class _Request:
            careerai_log_context = {"request_id": "r1", "actor_id": 7}

        class _Task:
            request = _Request()

        _restore_log_context(task_id="task-9", task=_Task())

        context = log.get_context()
        self.assertEqual(context.request_id, "r1")
        self.assertEqual(context.actor_id, 7)
        self.assertEqual(context.task_id, "task-9")

    def test_context_is_cleared_after_a_task_so_it_cannot_bleed(self):
        # Without this, a value set by one task persists into the next task on
        # the same pooled worker and one user's work is logged under another
        # user's identifiers.
        from config.celery import _clear_log_context, _restore_log_context

        class _Task:
            request = None

        _restore_log_context(task_id="task-1", task=_Task())
        _clear_log_context()

        self.assertEqual(log.get_context(), LogContext())

    def test_a_real_task_run_leaves_no_residue(self):
        from config.celery import ping

        log.bind(request_id="r1")
        ping.delay()

        self.assertIsNone(log.get_context().task_id)


class OutboxContextTests(TestCase):
    def tearDown(self):
        log.clear()

    def test_event_id_is_bound_while_a_consumer_runs(self):
        from apps.files.models import Attachment
        from apps.notifications import consumers, events
        from apps.notifications.services import outbox

        seen: list[int | None] = []

        def recorder(event):
            seen.append(log.get_context().event_id)

        recorder.__module__ = "tests"
        recorder.__qualname__ = "recorder_event_ctx"
        consumers._SUBSCRIBERS["file.scan_failed"] = [recorder]
        self.addCleanup(consumers._SUBSCRIBERS.pop, "file.scan_failed", None)

        attachment = Attachment.objects.create(
            storage_key="ab/cd/e.pdf",
            filename="e.pdf",
            mime="application/pdf",
            size=1,
            sha256="e" * 64,
        )
        event = events.emit(
            "file.scan_failed",
            attachment,
            payload={
                "attachment_public_id": str(attachment.public_id),
                "filename": "e.pdf",
                "scan_status": "error",
            },
        )
        outbox.drain()

        self.assertEqual(seen, [event.pk])

    def test_event_id_is_released_after_dispatch(self):
        self.assertIsNone(log.get_context().event_id)
