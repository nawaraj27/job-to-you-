"""
Authorization on a CV (SRS §2.5, ADR-A38).

Written from the failing side, and the distinction under test throughout is
**NotFound rather than NotAllowed**: a 403 on someone else's CV or export
confirms that it exists, and a CV is the most personal document this product
holds.

These rules were carried by their callers' tests until P7; the policy layer has
its own floor (DEVELOPER.md §8.1) and now meets it directly.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.billing.tests import factories as billing_factories
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotFound
from apps.resumes import policies
from apps.resumes.models import VersionReason
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import renders as render_service
from apps.resumes.services import versions as version_service
from apps.resumes.tests.test_rendering import sample_content


class DocumentOwnershipTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A document needs a default template to be created at all.
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.document = document_service.create_document(
            profile=self.profile, actor=self.profile.user, name="Backend CV"
        )
        self.stranger = create_profile()

    def test_a_stranger_reading_a_cv_is_not_found_rather_than_forbidden(self):
        with self.assertRaises(NotFound):
            policies.require_document_owner(self.document, self.stranger)

    def test_an_anonymous_reader_is_not_found(self):
        with self.assertRaises(NotFound):
            policies.require_document_owner(self.document, None)

    def test_the_owner_is_allowed(self):
        self.assertIs(policies.require_document_owner(self.document, self.profile), self.document)

    def test_a_deleted_cv_is_not_editable(self):
        from apps.resumes.models import VersionReason
        from apps.resumes.services import versions as version_service

        version_service.finalize(
            document=self.document, actor=self.profile.user, reason=VersionReason.SAVED
        )
        document_service.delete(document=self.document, actor=self.profile.user)
        self.document.refresh_from_db()

        with self.assertRaises(InvalidState) as caught:
            policies.require_editable(self.document)
        self.assertIn("deleted", caught.exception.message)


class RenderOwnershipTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        billing_factories.put_on_pro(self.profile)  # exporting is a Pro entitlement (P18)
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Backend CV"
        )
        draft_service.autosave(draft=self.document.draft, content=sample_content())
        self.document.refresh_from_db()
        self.record = render_service.export(document=self.document, actor=self.actor)

    def test_a_stranger_cannot_open_someone_elses_export(self):
        with self.assertRaises(NotFound):
            policies.require_render_owner(self.record, create_profile())

    def test_an_anonymous_reader_cannot_open_an_export(self):
        with self.assertRaises(NotFound):
            policies.require_render_owner(self.record, None)

    def test_the_owner_can_open_their_own_export(self):
        self.assertIs(policies.require_render_owner(self.record, self.profile), self.record)

    def test_a_redacted_version_can_no_longer_be_exported(self):
        # The tombstone is the point of ADR-A38: erasure has to survive a
        # later export attempt, not merely delete the file that existed.
        version_service.redact(version=self.record.version, actor=self.actor)
        version = self.record.version
        version.refresh_from_db()

        with self.assertRaises(InvalidState) as caught:
            policies.require_renderable(version)
        self.assertIn("erased", caught.exception.message)

    def test_a_live_version_is_renderable(self):
        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        self.assertIsNone(policies.require_renderable(version))
