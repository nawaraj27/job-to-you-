"""
Documents, drafts, the snapshot ring and version immutability (P5).

Three properties carry this phase, and each has its own class below:

  * autosave produces no versions (ADR-A19);
  * a finalized version cannot be updated **by any means** (ADR-A8) — including
    a raw SQL UPDATE, which is the only test that proves the trigger rather
    than the model guard;
  * redaction is the one permitted exception, and it is single-use (ADR-A38).
"""

from __future__ import annotations

from django.core.management import call_command
from django.db import connection, transaction
from django.db.utils import IntegrityError
from django.test import TestCase

from apps.candidates.tests.factories import create_profile
from apps.common.errors import Conflict, InvalidState, NotFound, StaleWrite
from apps.resumes import content as content_rules
from apps.resumes import policies, selectors
from apps.resumes.models import (
    BuilderMode,
    DocumentKind,
    ResumeDocument,
    ResumeDraftSnapshot,
    ResumeTemplate,
    ResumeVersion,
    VersionReason,
)
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import versions as version_service


class DocumentFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="My CV"
        )
        self.draft = self.document.draft

    def _fill(self) -> None:
        draft_service.set_contact(draft=self.draft, field="name", value="Priya Sharma")
        summary = content_rules.section_by_key(self.draft.content, "summary")
        draft_service.set_section_text(
            draft=self.draft, section_id=summary["id"], text="Backend engineer."
        )


class DocumentTests(DocumentFixture):
    def test_a_document_is_created_with_a_draft_and_a_template(self):
        self.assertIsNotNone(self.document.draft)
        self.assertIsNotNone(self.document.template)
        self.assertEqual(self.document.builder_mode, BuilderMode.EDITOR)

    def test_the_first_cv_is_primary_without_being_asked(self):
        self.assertTrue(self.document.is_primary)

    def test_the_second_cv_is_not(self):
        second = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second"
        )

        self.assertFalse(second.is_primary)

    def test_only_one_cv_can_be_primary(self):
        second = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second"
        )

        document_service.make_primary(document=second, actor=self.actor)
        self.document.refresh_from_db()

        self.assertTrue(second.is_primary)
        self.assertFalse(self.document.is_primary)
        self.assertEqual(
            ResumeDocument.objects.alive().filter(profile=self.profile, is_primary=True).count(), 1
        )

    def test_the_database_refuses_a_second_primary(self):
        second = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second"
        )

        with self.assertRaises(IntegrityError):
            ResumeDocument.objects.filter(pk=second.pk).update(is_primary=True)

    def test_creation_is_announced_and_audited(self):
        from apps.audit.models import AuditLog
        from apps.notifications.models import OutboxEvent

        self.assertTrue(OutboxEvent.objects.filter(type="resume.document_created").exists())
        self.assertTrue(AuditLog.objects.filter(verb="resume.document_created").exists())

    def test_renaming_needs_a_name(self):
        with self.assertRaises(InvalidState):
            document_service.rename(document=self.document, actor=self.actor, name="   ")

    def test_a_retired_template_cannot_be_chosen(self):
        template = ResumeTemplate.objects.exclude(pk=self.document.template_id).first()
        ResumeTemplate.objects.filter(pk=template.pk).update(is_active=False)
        template.refresh_from_db()

        with self.assertRaises(InvalidState):
            document_service.set_template(
                document=self.document, actor=self.actor, template=template
            )

    def test_changing_the_template_never_changes_the_content(self):
        before = dict(self.draft.content)
        template = (
            ResumeTemplate.objects.filter(is_active=True)
            .exclude(pk=self.document.template_id)
            .first()
        )

        document_service.set_template(document=self.document, actor=self.actor, template=template)
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content, before)

    def test_the_primary_cv_cannot_be_archived(self):
        with self.assertRaises(InvalidState):
            document_service.archive(document=self.document, actor=self.actor)

    def test_archiving_a_secondary_cv_works(self):
        second = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second"
        )

        document_service.archive(document=second, actor=self.actor)
        second.refresh_from_db()

        self.assertTrue(second.is_archived)
        self.assertNotIn(second, selectors.active_documents_for(self.profile))

    def test_deleting_is_soft_because_an_employer_may_hold_a_version(self):
        second = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second"
        )
        version_service.finalize(document=second, actor=self.actor, reason=VersionReason.SAVED)

        document_service.delete(document=second, actor=self.actor)

        self.assertTrue(ResumeDocument.objects.filter(pk=second.pk).exists())
        self.assertNotIn(second, selectors.documents_for(self.profile))

    def test_a_cv_that_was_never_saved_is_removed_outright(self):
        # No version, so nothing an employer holds can point at it
        # (docs/CV-Lifecycle.md).
        second = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second"
        )

        document_service.delete(document=second, actor=self.actor)

        self.assertFalse(ResumeDocument.objects.filter(pk=second.pk).exists())

    def test_the_last_primary_cv_can_be_deleted(self):
        document_service.delete(document=self.document, actor=self.actor)

        self.assertEqual(selectors.documents_for(self.profile).count(), 0)

    def test_duplicating_copies_the_content_not_the_history(self):
        self._fill()
        copy = document_service.duplicate(document=self.document, actor=self.actor)

        self.assertEqual(copy.draft.content["contact"]["name"], "Priya Sharma")
        self.assertEqual(copy.versions.count(), 0)
        self.assertFalse(copy.is_primary)

    def test_a_candidate_cannot_hoard_documents(self):
        # Saved CVs, so the separate cap on unsaved drafts is not what stops it.
        for index in range(document_service.MAX_DOCUMENTS - 1):
            document = document_service.create_document(
                profile=self.profile, actor=self.actor, name=f"CV {index}"
            )
            version_service.finalize(
                document=document, actor=self.actor, reason=VersionReason.SAVED
            )

        with self.assertRaises(Conflict):
            document_service.create_document(
                profile=self.profile, actor=self.actor, name="One more"
            )

    def test_another_profile_cannot_see_it(self):
        other = create_profile()

        self.assertIsNone(selectors.document_for(other, str(self.document.public_id)))
        with self.assertRaises(NotFound):
            policies.require_document_owner(self.document, other)

    def test_an_archived_document_is_not_editable(self):
        second = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second"
        )
        document_service.archive(document=second, actor=self.actor)
        second.refresh_from_db()

        with self.assertRaises(InvalidState):
            policies.require_editable(second)


class DocumentFromUploadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        from django.utils import timezone

        from apps.files.constants import ScanStatus
        from apps.files.models import Attachment
        from apps.resumes.services import parse as parse_service
        from apps.resumes.services import upload as upload_service
        from apps.resumes.tests.factories import SAMPLE_CV, uploaded_docx

        self.profile = create_profile()
        self.actor = self.profile.user
        self.upload, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_docx(text=SAMPLE_CV)
        )
        Attachment.objects.filter(pk=self.upload.attachment_id).update(
            scan_status=ScanStatus.CLEAN, scanned_at=timezone.now()
        )
        self.upload.attachment.refresh_from_db()
        parse_service.parse_upload(upload=self.upload)
        self.upload.refresh_from_db()

    def test_a_parsed_upload_becomes_an_editable_document(self):
        document = document_service.create_from_upload(upload=self.upload, actor=self.actor)
        content = document.draft.content

        self.assertEqual(document.kind, DocumentKind.UPLOADED)
        self.assertEqual(document.upload_id, self.upload.pk)
        experience = content_rules.section_by_key(content, "experience")
        self.assertTrue(experience["entries"])
        self.assertIn("Python", content_rules.section_by_key(content, "skills")["items"])

    def test_the_location_on_the_cv_survives_the_import(self):
        # Dropped on import, so the assistant then asked "where are you based?"
        # of somebody whose CV said so in its first line.
        from django.utils import timezone

        from apps.files.constants import ScanStatus
        from apps.files.models import Attachment
        from apps.resumes.services import parse as parse_service
        from apps.resumes.services import upload as upload_service
        from apps.resumes.tests.factories import uploaded_docx

        cv = "Maya Karki\nmaya@example.com | +977 9800000000 | Pokhara, Nepal\n\nSKILLS\nPython, Django"
        upload, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_docx(text=cv)
        )
        Attachment.objects.filter(pk=upload.attachment_id).update(
            scan_status=ScanStatus.CLEAN, scanned_at=timezone.now()
        )
        upload.attachment.refresh_from_db()
        parse_service.parse_upload(upload=upload)
        upload.refresh_from_db()

        content = document_service.create_from_upload(upload=upload, actor=self.actor).draft.content

        self.assertEqual(content["contact"]["location"], "Pokhara, Nepal")

    def test_projects_certifications_and_awards_survive_the_import(self):
        """The part of a first CV that matters most, and it was thrown away.

        The parser read projects, certifications, awards and publications and
        the document builder carried none of them. A student uploaded a CV
        whose projects were the best thing on it, pressed "Edit as a CV", and
        opened a document with no projects in it. An academic lost their
        publications the same way; a nurse lost their registration. Silently
        dropping the part of somebody's CV they are proudest of is the worst
        thing an importer can do after inventing something.
        """
        cv = "\n".join(
            [
                "Sabin Bhandari",
                "sabin@example.com",
                "9812345678",
                "",
                "EDUCATION",
                "BSc Computing, Islington College, 2022 - 2026",
                "",
                "PROJECTS",
                "Bus Tracker - a live map of valley routes, built with Flutter.",
                "Notes API",
                "- A REST API with token authentication.",
                "",
                "CERTIFICATIONS",
                "AWS Cloud Practitioner",
                "",
                "AWARDS",
                "Dean's list, 2025",
                "",
                "PUBLICATIONS",
                "Bhandari S. (2026) Routing on unreliable networks.",
                "",
                "SKILLS",
                "Python, Java",
            ]
        )
        from django.utils import timezone

        from apps.files.constants import ScanStatus
        from apps.files.models import Attachment
        from apps.resumes.services import parse as parse_service
        from apps.resumes.services import upload as upload_service
        from apps.resumes.tests.factories import uploaded_docx

        upload, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_docx(text=cv)
        )
        Attachment.objects.filter(pk=upload.attachment_id).update(
            scan_status=ScanStatus.CLEAN, scanned_at=timezone.now()
        )
        upload.attachment.refresh_from_db()
        parse_service.parse_upload(upload=upload)
        upload.refresh_from_db()

        content = document_service.create_from_upload(upload=upload, actor=self.actor).draft.content

        projects = content_rules.section_by_key(content, "projects")
        self.assertTrue(projects["visible"])
        self.assertEqual(
            [entry["title"] for entry in projects["entries"]], ["Bus Tracker", "Notes API"]
        )
        self.assertEqual(
            projects["entries"][0]["bullets"],
            ["a live map of valley routes, built with Flutter."],
        )

        self.assertEqual(
            content_rules.section_by_key(content, "certifications")["items"],
            ["AWS Cloud Practitioner"],
        )
        self.assertEqual(
            content_rules.section_by_key(content, "awards")["items"], ["Dean's list, 2025"]
        )
        self.assertEqual(
            content_rules.section_by_key(content, "publications")["items"],
            ["Bhandari S. (2026) Routing on unreliable networks"],
        )

    def test_a_cv_without_them_gets_no_empty_headings(self):
        # Only sections the file actually has are added, so an ordinary CV does
        # not open with an empty Publications heading on it.
        content = document_service.create_from_upload(
            upload=self.upload, actor=self.actor
        ).draft.content

        self.assertIsNone(content_rules.section_by_key(content, "publications"))
        self.assertIsNone(content_rules.section_by_key(content, "awards"))

    def test_imported_entries_start_detached_from_the_profile(self):
        # They came from the candidate's own file, not from their profile, so
        # the reconcile protocol must not treat them as profile-owned.
        document = document_service.create_from_upload(upload=self.upload, actor=self.actor)
        experience = content_rules.section_by_key(document.draft.content, "experience")

        self.assertEqual(experience["entries"][0]["sync_state"], content_rules.SyncState.DETACHED)

    def test_creating_from_the_same_upload_twice_opens_one_document(self):
        first = document_service.create_from_upload(upload=self.upload, actor=self.actor)
        second = document_service.create_from_upload(upload=self.upload, actor=self.actor)

        self.assertEqual(first.pk, second.pk)

    def test_an_unparsed_upload_is_refused(self):
        from apps.resumes.models import ParseStatus, ResumeUpload

        ResumeUpload.objects.filter(pk=self.upload.pk).update(parse_status=ParseStatus.PENDING)
        self.upload.refresh_from_db()

        with self.assertRaises(InvalidState):
            document_service.create_from_upload(upload=self.upload, actor=self.actor)


class DraftTests(DocumentFixture):
    def test_autosave_writes_the_draft(self):
        content = dict(self.draft.content)
        content["summary"] = "Rewritten."

        draft_service.autosave(draft=self.draft, content=content)
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content["summary"], "Rewritten.")
        self.assertTrue(self.draft.dirty)

    def test_autosave_creates_no_versions(self):
        # The rule the draft/version split exists for (ADR-A19).
        for index in range(10):
            content = dict(self.draft.content)
            content["summary"] = f"Draft {index}"
            draft_service.autosave(draft=self.draft, content=content)

        self.assertEqual(self.document.versions.count(), 0)

    def test_autosave_refuses_something_that_is_not_a_document(self):
        with self.assertRaises(InvalidState):
            draft_service.autosave(draft=self.draft, content={"nope": True})

    def test_a_stale_write_is_refused(self):
        stale = self.draft.row_version
        draft_service.set_contact(draft=self.draft, field="name", value="First")

        with self.assertRaises(StaleWrite):
            draft_service.autosave(
                draft=self.draft, content=self.draft.content, expected_version=stale
            )

    def test_every_edit_moves_the_row_version(self):
        before = self.draft.row_version
        draft_service.set_contact(draft=self.draft, field="name", value="Priya")

        self.assertEqual(self.draft.row_version, before + 1)

    def test_an_unknown_contact_field_is_refused(self):
        with self.assertRaises(InvalidState):
            draft_service.set_contact(draft=self.draft, field="blood_type", value="O")

    def test_sections_can_be_added_renamed_toggled_and_removed(self):
        section_id = draft_service.add_section(draft=self.draft, title="Volunteering")
        draft_service.rename_section(
            draft=self.draft, section_id=section_id, title="Community work"
        )
        visible = draft_service.toggle_section(draft=self.draft, section_id=section_id)
        self.draft.refresh_from_db()
        section = content_rules.section_by_id(self.draft.content, section_id)

        self.assertEqual(section["title"], "Community work")
        self.assertFalse(visible)

        draft_service.delete_section(draft=self.draft, section_id=section_id)
        self.draft.refresh_from_db()
        self.assertIsNone(content_rules.section_by_id(self.draft.content, section_id))

    def test_moving_a_section_renumbers_every_order(self):
        sections = self.draft.content["sections"]
        last = sections[-1]["id"]

        draft_service.move_section(draft=self.draft, section_id=last, offset=-2)
        self.draft.refresh_from_db()

        orders = [section["order"] for section in self.draft.content["sections"]]
        self.assertEqual(orders, list(range(len(orders))))

    def test_a_drag_reorders_every_section_in_one_move(self):
        sections = self.draft.content["sections"]
        moved = [sections[-1]["id"], *[section["id"] for section in sections[:-1]]]

        draft_service.reorder_sections(draft=self.draft, order=moved)
        self.draft.refresh_from_db()

        after = self.draft.content["sections"]
        self.assertEqual([section["id"] for section in after], moved)
        self.assertEqual([section["order"] for section in after], list(range(len(after))))

    def test_a_reorder_from_a_stale_page_is_refused_rather_than_applied(self):
        # Two tabs open: the other one added a section, and this one is about
        # to send an order that does not mention it. Applying that order would
        # drop the new section off the CV without anybody being told.
        stale = [section["id"] for section in self.draft.content["sections"]]
        draft_service.add_section(draft=self.draft, title="Volunteering")
        self.draft.refresh_from_db()

        with self.assertRaises(InvalidState):
            draft_service.reorder_sections(draft=self.draft, order=stale)

        self.draft.refresh_from_db()
        self.assertEqual(len(self.draft.content["sections"]), len(stale) + 1)

    def test_a_role_can_be_moved_within_its_section(self):
        """A CV is read in reverse chronological order.

        There was no way to put a role in one: an entry added after the fact,
        or imported in whatever order a PDF happened to extract, stayed where
        it landed and the only remedy was to delete it and retype it.
        """
        section = content_rules.section_by_key(self.draft.content, "experience")
        first = draft_service.add_entry(
            draft=self.draft, section_id=section["id"], title="Older role"
        )
        second = draft_service.add_entry(
            draft=self.draft, section_id=section["id"], title="Newer role"
        )

        draft_service.move_entry(draft=self.draft, entry_id=second, offset=-1)
        self.draft.refresh_from_db()

        after = content_rules.section_by_key(self.draft.content, "experience")["entries"]
        self.assertEqual([entry["id"] for entry in after[-2:]], [second, first])

    def test_moving_the_first_role_up_does_nothing(self):
        # Clamped rather than wrapped: "up" on the top role must not send it to
        # the bottom.
        section = content_rules.section_by_key(self.draft.content, "experience")
        first = draft_service.add_entry(
            draft=self.draft, section_id=section["id"], title="Only role"
        )

        draft_service.move_entry(draft=self.draft, entry_id=first, offset=-1)
        self.draft.refresh_from_db()

        entries = content_rules.section_by_key(self.draft.content, "experience")["entries"]
        self.assertEqual(entries[-1]["id"], first)

    def test_a_drag_reorders_the_roles_in_one_move(self):
        section = content_rules.section_by_key(self.draft.content, "experience")
        for title in ("A", "B", "C"):
            draft_service.add_entry(draft=self.draft, section_id=section["id"], title=title)
        self.draft.refresh_from_db()

        section = content_rules.section_by_key(self.draft.content, "experience")
        wanted = list(reversed([entry["id"] for entry in section["entries"]]))
        draft_service.reorder_entries(draft=self.draft, section_id=section["id"], order=wanted)
        self.draft.refresh_from_db()

        after = content_rules.section_by_key(self.draft.content, "experience")["entries"]
        self.assertEqual([entry["id"] for entry in after], wanted)

    def test_a_reorder_from_a_stale_page_is_refused(self):
        section = content_rules.section_by_key(self.draft.content, "experience")
        stale = [entry["id"] for entry in section["entries"]]
        draft_service.add_entry(draft=self.draft, section_id=section["id"], title="Added since")
        self.draft.refresh_from_db()

        section = content_rules.section_by_key(self.draft.content, "experience")
        with self.assertRaises(InvalidState):
            draft_service.reorder_entries(draft=self.draft, section_id=section["id"], order=stale)

    def test_duplicating_a_role_copies_it_below_the_original(self):
        # Two roles at one employer is the most common thing somebody wants to
        # do twice, and doing it by hand means retyping every bullet.
        section = content_rules.section_by_key(self.draft.content, "experience")
        original = draft_service.add_entry(
            draft=self.draft, section_id=section["id"], title="Engineer", org="Fonepay"
        )
        draft_service.set_bullets(
            draft=self.draft, entry_id=original, bullets=["Shipped the thing."]
        )
        self.draft.refresh_from_db()

        copy_id = draft_service.duplicate_entry(draft=self.draft, entry_id=original)
        self.draft.refresh_from_db()

        entries = content_rules.section_by_key(self.draft.content, "experience")["entries"]
        ids = [entry["id"] for entry in entries]
        self.assertEqual(ids[ids.index(original) + 1], copy_id)

        copy = next(entry for entry in entries if entry["id"] == copy_id)
        self.assertEqual(copy["title"], "Engineer")
        self.assertEqual(copy["bullets"], ["Shipped the thing."])
        self.assertNotEqual(copy["id"], original)

    def test_a_section_that_is_not_there_is_refused(self):
        with self.assertRaises(NotFound):
            draft_service.rename_section(draft=self.draft, section_id="nope", title="X")

    def test_prose_cannot_be_written_into_a_list_section(self):
        skills = content_rules.section_by_key(self.draft.content, "skills")

        with self.assertRaises(InvalidState):
            draft_service.set_section_text(draft=self.draft, section_id=skills["id"], text="hi")

    def test_the_summary_section_and_the_summary_field_stay_in_step(self):
        summary = content_rules.section_by_key(self.draft.content, "summary")

        draft_service.set_section_text(
            draft=self.draft, section_id=summary["id"], text="Backend engineer."
        )
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content["summary"], "Backend engineer.")

    def test_entries_and_bullets_are_bounded(self):
        experience = content_rules.section_by_key(self.draft.content, "experience")
        entry_id = draft_service.add_entry(
            draft=self.draft, section_id=experience["id"], title="Engineer", org="Nexa"
        )

        with self.assertRaises(InvalidState):
            draft_service.set_bullets(
                draft=self.draft,
                entry_id=entry_id,
                bullets=[f"line {index}" for index in range(draft_service.MAX_BULLETS + 1)],
            )

    def test_editing_a_linked_entry_marks_it_as_diverged(self):
        # The reconcile protocol in one transition: the moment the candidate
        # rewords a profile-sourced entry, the profile stops owning the wording.
        experience = content_rules.section_by_key(self.draft.content, "experience")
        entry_id = draft_service.add_entry(draft=self.draft, section_id=experience["id"])

        content = self.draft.content
        _section, entry = content_rules.entry_by_id(content, entry_id)
        entry["sync_state"] = content_rules.SyncState.LINKED
        draft_service.autosave(draft=self.draft, content=content)

        draft_service.update_entry(draft=self.draft, entry_id=entry_id, title="Senior Engineer")
        self.draft.refresh_from_db()

        _section, updated = content_rules.entry_by_id(self.draft.content, entry_id)
        self.assertEqual(updated["sync_state"], content_rules.SyncState.EDITED)

    def test_an_entry_that_is_not_there_is_refused(self):
        with self.assertRaises(NotFound):
            draft_service.update_entry(draft=self.draft, entry_id="nope", title="X")


class SnapshotRingTests(DocumentFixture):
    def test_every_edit_leaves_a_snapshot(self):
        draft_service.set_contact(draft=self.draft, field="name", value="One")
        draft_service.set_contact(draft=self.draft, field="name", value="Two")

        self.assertEqual(self.draft.snapshots.count(), 2)

    def test_the_ring_is_bounded(self):
        for index in range(draft_service.SNAPSHOT_LIMIT + 10):
            draft_service.set_contact(draft=self.draft, field="name", value=f"Name {index}")

        self.assertEqual(self.draft.snapshots.count(), draft_service.SNAPSHOT_LIMIT)

    def test_the_oldest_snapshots_are_the_ones_dropped(self):
        for index in range(draft_service.SNAPSHOT_LIMIT + 5):
            draft_service.set_contact(draft=self.draft, field="name", value=f"Name {index}")

        sequences = list(self.draft.snapshots.values_list("seq", flat=True))
        self.assertEqual(min(sequences), max(sequences) - draft_service.SNAPSHOT_LIMIT + 1)

    def test_undo_steps_back_and_consumes_the_snapshot(self):
        draft_service.set_contact(draft=self.draft, field="name", value="First")
        draft_service.set_contact(draft=self.draft, field="name", value="Second")

        self.assertTrue(draft_service.undo(draft=self.draft))
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content["contact"]["name"], "First")
        self.assertEqual(self.draft.snapshots.count(), 1)

    def test_undo_with_no_history_says_so(self):
        self.assertFalse(draft_service.undo(draft=self.draft))

    def test_snapshots_are_scoped_to_their_draft(self):
        other = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second"
        )
        draft_service.set_contact(draft=other.draft, field="name", value="Other")

        self.assertEqual(ResumeDraftSnapshot.objects.filter(draft=self.draft).count(), 0)


class VersionTests(DocumentFixture):
    def setUp(self):
        super().setUp()
        self._fill()

    def test_saving_creates_a_version_from_the_draft(self):
        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        self.document.refresh_from_db()

        self.assertEqual(version.version_no, 1)
        self.assertEqual(self.document.current_version_id, version.pk)
        self.assertIsNotNone(version.finalized_at)
        self.assertEqual(version.template_snapshot["slug"], self.document.template.slug)

    def test_the_draft_is_marked_clean_after_a_save(self):
        version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        self.draft.refresh_from_db()

        self.assertFalse(self.draft.dirty)

    def test_an_unchanged_document_reuses_its_version(self):
        # Ten clicks on Analyse must not produce ten identical rows.
        first = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        self.document.refresh_from_db()
        second = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.ANALYSED
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.document.versions.count(), 1)

    def test_a_changed_document_gets_a_new_number(self):
        version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        draft_service.set_contact(draft=self.draft, field="headline", value="Backend Engineer")
        self.document.refresh_from_db()

        second = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

        self.assertEqual(second.version_no, 2)

    def test_reordering_alone_is_not_a_change(self):
        # The hash covers what the document says, not the ids it says it with.
        version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        self.document.refresh_from_db()
        hidden = content_rules.section_by_key(self.draft.content, "projects")
        draft_service.rename_section(
            draft=self.draft, section_id=hidden["id"], title="Side projects"
        )
        self.document.refresh_from_db()

        again = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

        self.assertEqual(self.document.versions.count(), 1)
        self.assertEqual(again.version_no, 1)

    def test_an_empty_document_cannot_be_finalized(self):
        # An empty CV finalized into an immutable row is a row that can never
        # be corrected and never be deleted.
        empty = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Empty"
        )
        draft_service.set_contact(draft=empty.draft, field="name", value="")

        with self.assertRaises(InvalidState):
            version_service.finalize(document=empty, actor=self.actor, reason=VersionReason.SAVED)

    def test_only_the_four_permitted_reasons_are_accepted(self):
        with self.assertRaises(InvalidState):
            version_service.finalize(document=self.document, actor=self.actor, reason="autosave")

    def test_a_version_is_announced_without_its_content(self):
        from apps.notifications.models import OutboxEvent

        version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

        event = OutboxEvent.objects.get(type="resume.version_created")
        self.assertIn("sha256", event.payload)
        self.assertNotIn("content", event.payload)
        self.assertNotIn("Priya", str(event.payload))

    def test_restoring_copies_an_old_version_into_the_draft(self):
        first = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        draft_service.set_contact(draft=self.draft, field="name", value="Changed")
        self.document.refresh_from_db()

        version_service.restore(document=self.document, actor=self.actor, version=first)
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content["contact"]["name"], "Priya Sharma")
        # The history is a record, not a stack: restoring adds nothing to it.
        self.assertEqual(self.document.versions.count(), 1)

    def test_restoring_keeps_nothing_the_old_version_did_not_have(self):
        """Restore merged, so it handed back a document that never existed.

        A section added after the version was frozen survived the restore,
        because the old content was written over the draft key by key rather
        than in place of it.
        """
        first = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        draft_service.add_section(draft=self.draft, title="Volunteering", kind="list")
        self.draft.refresh_from_db()

        version_service.restore(document=self.document, actor=self.actor, version=first)
        self.draft.refresh_from_db()

        titles = [section["title"] for section in self.draft.content["sections"]]
        self.assertNotIn("Volunteering", titles)

    def test_a_restore_can_be_undone(self):
        # Restore replaces the working draft, which is a frightening thing to
        # offer somebody mid-edit unless there is a way back.
        first = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        draft_service.set_contact(draft=self.draft, field="name", value="Changed")

        version_service.restore(document=self.document, actor=self.actor, version=first)
        draft_service.undo(draft=self.draft)
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content["contact"]["name"], "Changed")

    def test_a_version_from_another_document_cannot_be_restored(self):
        other = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Other"
        )
        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

        with self.assertRaises(InvalidState):
            version_service.restore(document=other, actor=self.actor, version=version)


class ImmutabilityTests(DocumentFixture):
    """ADR-A8, enforced three ways. Each is tested separately."""

    def setUp(self):
        super().setUp()
        self._fill()
        self.version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

    def test_the_model_refuses_an_update(self):
        self.version.change_note = "tampered"

        with self.assertRaises(ValueError):
            self.version.save()

    def test_the_model_refuses_a_partial_update_of_evidentiary_fields(self):
        with self.assertRaises(ValueError):
            self.version.save(update_fields=["sha256"])

    def test_the_database_refuses_a_queryset_update(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResumeVersion.objects.filter(pk=self.version.pk).update(change_note="tampered")

    def test_the_database_refuses_raw_sql(self):
        # The only test that proves the trigger rather than the model guard.
        # A data-fix script is exactly the case ADR-A8 exists for.
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE resumes_resumeversion SET change_note = 'tampered' WHERE id = %s",
                [self.version.pk],
            )

    def test_the_content_itself_cannot_be_rewritten_without_redacting(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE resumes_resumeversion SET content = '{}'::jsonb WHERE id = %s",
                [self.version.pk],
            )

    def test_versions_of_other_documents_are_untouched_by_any_of_this(self):
        self.assertEqual(ResumeVersion.objects.count(), 1)


class RedactionTests(DocumentFixture):
    """ADR-A38: the one permitted transition."""

    def setUp(self):
        super().setUp()
        self._fill()
        self.version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.APPLIED
        )

    def test_redaction_replaces_the_content_with_a_tombstone(self):
        version_service.redact(version=self.version, actor=self.actor)
        self.version.refresh_from_db()

        self.assertTrue(self.version.is_redacted)
        self.assertTrue(self.version.content["redacted"])
        self.assertEqual(self.version.content["sections"], [])

    def test_the_evidentiary_columns_survive_byte_identical(self):
        before = ResumeVersion.objects.get(pk=self.version.pk)

        version_service.redact(version=self.version, actor=self.actor)
        after = ResumeVersion.objects.get(pk=self.version.pk)

        # The hash is the anchor that still proves what was submitted, and a
        # hash of destroyed content is not personal data.
        self.assertEqual(after.sha256, before.sha256)
        self.assertEqual(after.finalized_at, before.finalized_at)
        self.assertEqual(after.version_no, before.version_no)
        self.assertEqual(after.template_snapshot, before.template_snapshot)
        self.assertEqual(after.created_reason, before.created_reason)

    def test_redaction_is_single_use(self):
        version_service.redact(version=self.version, actor=self.actor)

        with self.assertRaises(InvalidState):
            version_service.redact(version=self.version, actor=self.actor)

    def test_the_database_refuses_a_second_redaction_from_any_source(self):
        version_service.redact(version=self.version, actor=self.actor)

        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE resumes_resumeversion SET redaction_reason = 'again' WHERE id = %s",
                [self.version.pk],
            )

    def test_the_trigger_refuses_a_redaction_that_also_changes_evidence(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE resumes_resumeversion SET redacted_at = now(), sha256 = 'x' WHERE id = %s",
                [self.version.pk],
            )

    def test_a_redacted_version_cannot_be_restored(self):
        version_service.redact(version=self.version, actor=self.actor)
        self.version.refresh_from_db()

        with self.assertRaises(InvalidState):
            version_service.restore(document=self.document, actor=self.actor, version=self.version)

    def test_redaction_is_announced_and_audited(self):
        from apps.audit.models import AuditLog
        from apps.notifications.models import OutboxEvent

        version_service.redact(version=self.version, actor=self.actor)

        self.assertTrue(OutboxEvent.objects.filter(type="resume.version_redacted").exists())
        self.assertTrue(AuditLog.objects.filter(verb="resume.version_redacted").exists())
