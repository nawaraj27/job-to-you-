"""Audit log behaviour (SRS NFR-9)."""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.audit import selectors, services
from apps.audit.models import AuditLog
from apps.files.models import Attachment


def _attachment() -> Attachment:
    return Attachment.objects.create(
        storage_key="ab/cd/abcd.pdf",
        filename="cv.pdf",
        mime="application/pdf",
        size=10,
        sha256="b" * 64,
    )


class AuditWriteTests(TestCase):
    def setUp(self):
        self.user = create_user()

    def test_record_captures_actor_target_and_label(self):
        attachment = _attachment()

        entry = services.record(verb="attachment.quarantined", actor=self.user, target=attachment)

        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.target, attachment)
        self.assertEqual(entry.target_repr, str(attachment))

    def test_target_repr_survives_the_target_being_deleted(self):
        # An audit entry whose subject has been deleted must still be readable,
        # which is why the label is denormalised at write time.
        attachment = _attachment()
        services.record(verb="attachment.deleted", actor=self.user, target=attachment)
        label = str(attachment)
        attachment.delete()

        entry = AuditLog.objects.get()
        self.assertEqual(entry.target_repr, label)
        self.assertIsNone(entry.target)

    def test_secrets_are_scrubbed_at_any_depth(self):
        entry = services.record(
            verb="user.authenticated",
            actor=self.user,
            metadata={
                "password": "hunter2",
                "nested": {"api_key": "sk-live-123", "kept": "value"},
            },
        )

        self.assertEqual(entry.metadata["password"], "[redacted]")
        self.assertEqual(entry.metadata["nested"]["api_key"], "[redacted]")
        self.assertEqual(entry.metadata["nested"]["kept"], "value")

    def test_system_actions_have_no_actor(self):
        entry = services.record(verb="outbox.purged")
        self.assertIsNone(entry.actor)


class AuditImmutabilityTests(TestCase):
    def test_an_entry_cannot_be_updated(self):
        entry = services.record(verb="job.published")
        entry.verb = "job.deleted"
        with self.assertRaises(ValueError):
            entry.save()

    def test_an_entry_cannot_be_deleted(self):
        entry = services.record(verb="job.published")
        with self.assertRaises(ValueError):
            entry.delete()

    def test_the_stored_row_is_unchanged_after_a_refused_update(self):
        entry = services.record(verb="job.published")
        entry.verb = "tampered"
        with self.assertRaises(ValueError):
            entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.verb, "job.published")


class AuditReadTests(TestCase):
    def test_for_target_returns_that_object_only(self):
        first, second = _attachment(), _attachment()
        services.record(verb="attachment.deleted", target=first)
        services.record(verb="attachment.deleted", target=second)

        self.assertEqual(selectors.for_target(first).count(), 1)

    def test_for_actor_returns_that_user_only(self):
        user, other = create_user(), create_user()
        services.record(verb="job.published", actor=user)
        services.record(verb="job.published", actor=other)

        self.assertEqual(selectors.for_actor(user).count(), 1)
