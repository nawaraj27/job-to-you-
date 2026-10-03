"""Errors, identifiers, hashing and state machines."""

from __future__ import annotations

import io
import uuid

from django.test import SimpleTestCase

from apps.common.errors import (
    Conflict,
    DomainError,
    InvalidState,
    NotAllowed,
    NotFound,
    QuotaExceeded,
    StaleWrite,
)
from apps.common.hashing import sha256_bytes, sha256_stream, stable_hash
from apps.common.state_machine import StateMachine, Transition
from apps.common.uuid7 import uuid7


class DomainErrorTests(SimpleTestCase):
    def test_each_error_maps_to_its_http_status(self):
        expected = {
            NotAllowed: 403,
            NotFound: 404,
            InvalidState: 409,
            Conflict: 409,
            StaleWrite: 409,
            QuotaExceeded: 429,
        }
        for error_class, status in expected.items():
            with self.subTest(error=error_class.__name__):
                self.assertEqual(error_class().status_code, status)

    def test_stale_write_is_a_conflict(self):
        # Callers that handle conflicts generically must also catch stale writes.
        self.assertTrue(issubclass(StaleWrite, Conflict))

    def test_message_and_details_survive_to_the_response_shape(self):
        error = DomainError("Custom message", code="custom", details={"field": "title"})
        self.assertEqual(
            error.as_dict(),
            {"code": "custom", "message": "Custom message", "details": {"field": "title"}},
        )

    def test_default_message_is_used_when_none_given(self):
        self.assertEqual(NotFound().message, NotFound.default_message)


class UUID7Tests(SimpleTestCase):
    def test_version_and_variant_bits_are_correct(self):
        value = uuid7()
        self.assertEqual(value.version, 7)
        # RFC 9562 variant is 0b10xx in the top bits of octet 8.
        self.assertEqual((value.bytes[8] & 0xC0), 0x80)

    def test_values_are_monotonic_within_a_process(self):
        # The property that makes v7 worth using: ordered inserts, so a UUID
        # primary key does not scatter writes across the index.
        values = [uuid7() for _ in range(500)]
        self.assertEqual(values, sorted(values, key=lambda item: item.int))

    def test_values_are_unique(self):
        self.assertEqual(len({uuid7() for _ in range(2000)}), 2000)

    def test_is_a_real_uuid(self):
        self.assertIsInstance(uuid7(), uuid.UUID)


class HashingTests(SimpleTestCase):
    def test_stream_hash_matches_bytes_hash(self):
        payload = b"a resume, more or less"
        self.assertEqual(sha256_stream(io.BytesIO(payload)), sha256_bytes(payload))

    def test_stream_position_is_restored(self):
        # store() hashes an upload and then saves the same handle; a consumed
        # stream would silently write an empty file.
        stream = io.BytesIO(b"payload")
        stream.read(3)
        sha256_stream(stream)
        self.assertEqual(stream.tell(), 3)

    def test_stable_hash_ignores_key_order(self):
        self.assertEqual(stable_hash({"a": 1, "b": 2}), stable_hash({"b": 2, "a": 1}))

    def test_stable_hash_distinguishes_values(self):
        self.assertNotEqual(stable_hash({"a": 1}), stable_hash({"a": 2}))

    def test_stable_hash_handles_non_json_types(self):
        self.assertTrue(stable_hash({"when": uuid7()}))


class _Thing:
    def __init__(self, status: str) -> None:
        self.status = status


class StateMachineTests(SimpleTestCase):
    machine = StateMachine(
        "status",
        transitions=[
            Transition("draft", "published", permission="job.publish"),
            Transition("published", "paused"),
            Transition("paused", "published"),
        ],
    )

    def test_legal_transition_is_allowed(self):
        thing = _Thing("published")
        self.assertIsNotNone(self.machine.require(thing, "paused"))

    def test_illegal_transition_raises_invalid_state(self):
        thing = _Thing("draft")
        with self.assertRaises(InvalidState):
            self.machine.require(thing, "paused")

    def test_missing_permission_raises_not_allowed_not_invalid_state(self):
        # The two answers are different: "you cannot do that yet" versus "you
        # may not do that". Collapsing them produces misleading error messages.
        thing = _Thing("draft")
        with self.assertRaises(NotAllowed):
            self.machine.require(thing, "published", actor_permissions=[])

    def test_permission_granted_allows_transition(self):
        thing = _Thing("draft")
        transition = self.machine.require(thing, "published", actor_permissions=["job.publish"])
        self.assertEqual(transition.target, "published")

    def test_targets_from_lists_legal_moves(self):
        self.assertEqual(set(self.machine.targets_from("published")), {"paused"})

    def test_duplicate_transitions_are_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            StateMachine("status", [Transition("a", "b"), Transition("a", "b")])


class UUID7ClockTests(SimpleTestCase):
    """The two branches that keep identifiers ordered when the clock misbehaves."""

    def setUp(self):
        from apps.common import uuid7 as module

        self.module = module
        self._saved = (module._last_timestamp_ms, module._counter)

    def tearDown(self):
        self.module._last_timestamp_ms, self.module._counter = self._saved

    def test_ids_stay_ordered_when_the_clock_steps_backwards(self):
        # An NTP correction must not let a later id sort before an earlier one.
        from unittest import mock

        base = 1_700_000_000_000
        with mock.patch.object(self.module.time, "time_ns", return_value=base * 1_000_000):
            first = uuid7()
        with mock.patch.object(
            self.module.time, "time_ns", return_value=(base - 5_000) * 1_000_000
        ):
            second = uuid7()

        self.assertLess(first.int, second.int)

    def test_more_than_4096_ids_in_one_millisecond_stay_ordered(self):
        # rand_a is a 12-bit counter; beyond it the generator must roll forward
        # rather than repeat a window.
        from unittest import mock

        fixed = 1_700_000_000_000 * 1_000_000
        with mock.patch.object(self.module.time, "time_ns", return_value=fixed):
            values = [uuid7() for _ in range(4_100)]

        self.assertEqual(values, sorted(values, key=lambda item: item.int))
        self.assertEqual(len(set(values)), len(values))
