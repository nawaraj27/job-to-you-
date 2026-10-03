"""Event registry rules (SRS §13.1, DEVELOPER.md §4)."""

from __future__ import annotations

from django.test import TestCase

from apps.notifications import events


class RegistryTests(TestCase):
    def test_emitting_an_unregistered_event_is_refused(self):
        # Declaration is what keeps the SRS catalogue checkable against code.
        with self.assertRaises(events.UnregisteredEvent):
            events.emit("nothing.happened", payload={})

    def test_missing_required_payload_keys_are_refused(self):
        with self.assertRaises(events.InvalidEventPayload) as caught:
            events.emit("file.scan_failed", payload={"filename": "cv.pdf"})
        self.assertIn("attachment_public_id", str(caught.exception))

    def test_command_shaped_names_are_rejected(self):
        # Events are facts, not instructions. "application.submit" is a command
        # and would invite consumers that expect to control the outcome.
        for bad in ["Application.Submitted", "submitted", "a.b.c", "job published"]:
            with self.subTest(name=bad), self.assertRaises(ValueError):
                events.register(bad)

    def test_registering_the_same_spec_twice_is_harmless(self):
        spec = events.register("widget.reregistered", schema_version=1, required={"id"})
        again = events.register("widget.reregistered", schema_version=1, required={"id"})
        self.assertEqual(spec, again)

    def test_registering_a_conflicting_spec_is_refused(self):
        # A silent payload change is how a consumer starts failing in
        # production for reasons nobody can see in the diff.
        events.register("widget.conflicted", schema_version=1, required={"id"})
        with self.assertRaises(ValueError):
            events.register("widget.conflicted", schema_version=2, required={"id"})

    def test_platform_events_are_declared(self):
        self.assertIn("file.scan_failed", events.registry())

    def test_every_registered_event_carries_a_schema_version(self):
        for event_type, spec in events.registry().items():
            with self.subTest(event=event_type):
                self.assertGreaterEqual(spec.schema_version, 1)

    def test_emit_records_the_aggregate_and_schema_version(self):
        from apps.files.models import Attachment

        attachment = Attachment.objects.create(
            storage_key="ab/cd/abcd.pdf",
            filename="cv.pdf",
            mime="application/pdf",
            size=10,
            sha256="a" * 64,
        )
        event = events.emit(
            "file.scan_failed",
            attachment,
            payload={
                "attachment_public_id": str(attachment.public_id),
                "filename": "cv.pdf",
                "scan_status": "infected",
            },
        )

        self.assertEqual(event.schema_version, 1)
        self.assertEqual(event.aggregate, attachment)
