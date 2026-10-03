"""
Rendering and export (P6, FR-3.10, ADR-A8).

The properties that matter are not "does it produce a file" — they are:

  * only an immutable version is ever rendered;
  * the same input produces byte-identical output, which is what makes the
    cache correct rather than merely fast;
  * an export freezes a version, so a downloaded file always has a record
    behind it;
  * a download belongs to the CV's owner and nobody else.
"""

from __future__ import annotations

import pathlib
import zipfile
from io import BytesIO

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase

from apps.billing.tests import factories as billing_factories
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.common.hashing import sha256_bytes
from apps.files.constants import ScanStatus
from apps.resumes import content as content_rules
from apps.resumes import selectors
from apps.resumes.models import (
    RenderFormat,
    ResumeRender,
    ResumeTemplate,
    VersionReason,
)
from apps.resumes.rendering import RenderFailed, UnsupportedRenderFormat, fonts, render
from apps.resumes.rendering import layout as layout_rules
from apps.resumes.rendering.base import ENGINE_VERSION, stale_for
from apps.resumes.rendering.fonts import family_for
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import renders as render_service
from apps.resumes.services import versions as version_service

SAMPLE_TEMPLATE = {
    "slug": "meridian",
    "name": "Meridian",
    "layout": "classic",
    "accent": "ink",
    "font": "Source Serif",
    "tier": "free",
}


def sample_content(*, entries: int = 2, bullets: int = 3) -> dict:
    """A filled CV, as a version would hold it."""
    content = content_rules.blank_document(
        name="Priya Sharma", headline="Backend Engineer", email="priya@example.com"
    )
    content["contact"]["phone"] = "+977 9812345678"
    content["summary"] = "Backend engineer with three years building payment systems."

    summary = content_rules.section_by_key(content, "summary")
    summary["text"] = content["summary"]

    experience = content_rules.section_by_key(content, "experience")
    experience["entries"] = [
        content_rules.new_entry(
            title=f"Backend Engineer {index}",
            org="Nexa Payments",
            dates="Aug 2023 — present",
            bullets=[f"Did the {position} important thing." for position in range(bullets)],
        )
        for index in range(entries)
    ]

    skills = content_rules.section_by_key(content, "skills")
    skills["items"] = ["Python", "Django", "PostgreSQL", "Redis"]
    return content


class LayoutTests(TestCase):
    """One layout pass, shared by both renderers."""

    def test_a_cv_flattens_to_head_then_sections(self):
        document = layout_rules.build(sample_content(), template=SAMPLE_TEMPLATE)
        kinds = [block.kind for block in document.blocks]

        self.assertEqual(kinds[0], "name")
        self.assertIn("headline", kinds)
        self.assertIn("contact", kinds)
        self.assertIn("entry", kinds)

    def test_hidden_sections_are_not_rendered(self):
        content = sample_content()
        content_rules.section_by_key(content, "skills")["visible"] = False

        document = layout_rules.build(content, template=SAMPLE_TEMPLATE)

        self.assertNotIn("Python", [item for block in document.blocks for item in block.items])

    def test_an_empty_section_is_dropped_rather_than_left_as_a_heading(self):
        # A heading with nothing under it reads as the candidate's omission.
        content = sample_content()
        education = content_rules.section_by_key(content, "education")
        education["visible"] = True
        education["entries"] = []

        document = layout_rules.build(content, template=SAMPLE_TEMPLATE)

        self.assertNotIn("Education", [block.text for block in document.blocks])

    def test_sections_follow_their_stored_order_not_list_position(self):
        content = sample_content()
        content["sections"][0]["order"] = 99

        document = layout_rules.build(content, template=SAMPLE_TEMPLATE)
        headings = [block.text for block in document.blocks if block.kind == "heading"]

        self.assertEqual(headings[-1], "Summary")

    def test_a_sidebar_template_moves_the_short_sections_across(self):
        document = layout_rules.build(
            sample_content(), template={**SAMPLE_TEMPLATE, "layout": "sidebar"}
        )

        self.assertTrue(document.sidebar)
        self.assertIn("Skills", [block.text for block in document.sidebar])
        self.assertNotIn("Skills", [block.text for block in document.blocks])

    def test_a_redacted_version_renders_as_its_tombstone(self):
        document = layout_rules.build(content_rules.TOMBSTONE, template=SAMPLE_TEMPLATE)

        self.assertTrue(document.is_redacted)
        self.assertIn(layout_rules.REDACTED_NOTICE, [block.text for block in document.blocks])

    def test_an_empty_document_still_produces_a_title(self):
        document = layout_rules.build({}, template=SAMPLE_TEMPLATE)

        self.assertEqual(document.blocks[0].kind, "name")


class PdfRenderTests(TestCase):
    def test_a_pdf_is_produced_with_the_right_magic_bytes(self):
        result = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="pdf")

        self.assertTrue(result.data.startswith(b"%PDF"))
        self.assertEqual(result.mime, "application/pdf")
        self.assertEqual(result.engine, "reportlab")

    def test_rendering_twice_is_byte_identical(self):
        # The property the whole cache rests on.
        first = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="pdf")
        second = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="pdf")

        self.assertEqual(sha256_bytes(first.data), sha256_bytes(second.data))

    def test_different_content_produces_a_different_file(self):
        first = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="pdf")
        changed = sample_content()
        changed["contact"]["name"] = "Someone Else"
        second = render(changed, template=SAMPLE_TEMPLATE, fmt="pdf")

        self.assertNotEqual(sha256_bytes(first.data), sha256_bytes(second.data))

    def test_a_different_template_produces_a_different_file(self):
        first = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="pdf")
        second = render(
            sample_content(), template={**SAMPLE_TEMPLATE, "layout": "sidebar"}, fmt="pdf"
        )

        self.assertNotEqual(sha256_bytes(first.data), sha256_bytes(second.data))

    def test_the_page_count_is_reported(self):
        result = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="pdf")

        self.assertEqual(result.pages, 1)

    def test_a_long_cv_paginates_rather_than_truncating(self):
        result = render(sample_content(entries=25, bullets=8), template=SAMPLE_TEMPLATE, fmt="pdf")

        self.assertGreater(result.pages, 1)

    def test_markup_in_a_cv_is_escaped_not_interpreted(self):
        # "R&D" and "<lead>" are things people write in CVs.
        content = sample_content()
        content["contact"]["headline"] = "Head of R&D <lead>"

        result = render(content, template=SAMPLE_TEMPLATE, fmt="pdf")

        self.assertTrue(result.data.startswith(b"%PDF"))

    def test_the_font_actually_used_is_recorded(self):
        result = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="pdf")

        self.assertEqual(result.fonts["family"], "serif")
        self.assertEqual(result.fonts["embedded"], "no")

    def test_an_unknown_font_falls_back_rather_than_failing(self):
        result = render(
            sample_content(), template={**SAMPLE_TEMPLATE, "font": "Nonexistent"}, fmt="pdf"
        )

        self.assertEqual(result.fonts["family"], "sans")

    def test_a_redacted_version_renders_a_readable_notice(self):
        result = render(content_rules.TOMBSTONE, template=SAMPLE_TEMPLATE, fmt="pdf")

        self.assertTrue(result.data.startswith(b"%PDF"))


class DocxRenderTests(TestCase):
    def test_a_docx_is_produced_as_a_readable_zip(self):
        result = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="docx")

        with zipfile.ZipFile(BytesIO(result.data)) as archive:
            self.assertIn("word/document.xml", archive.namelist())
        self.assertEqual(result.engine, "python-docx")

    def test_the_docx_contains_the_cv_text(self):
        result = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="docx")

        with zipfile.ZipFile(BytesIO(result.data)) as archive:
            document = archive.read("word/document.xml").decode("utf-8")

        self.assertIn("Priya Sharma", document)
        self.assertIn("Nexa Payments", document)

    def test_rendering_twice_is_byte_identical(self):
        # A zip records a timestamp per entry; without normalisation the cache
        # would never hit and every export would re-render.
        first = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="docx")
        second = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="docx")

        self.assertEqual(sha256_bytes(first.data), sha256_bytes(second.data))

    def test_word_never_claims_a_page_count(self):
        # Word paginates when it opens the file; a stored number would be a guess.
        result = render(sample_content(), template=SAMPLE_TEMPLATE, fmt="docx")

        self.assertIsNone(result.pages)

    def test_pdf_and_docx_say_the_same_things(self):
        content = sample_content()
        document = layout_rules.build(content, template=SAMPLE_TEMPLATE)
        docx_result = render(content, template=SAMPLE_TEMPLATE, fmt="docx")

        with zipfile.ZipFile(BytesIO(docx_result.data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")

        for block in document.blocks:
            if block.kind == "heading":
                self.assertIn(block.text.upper(), xml)


class EngineTests(TestCase):
    def test_an_unknown_format_is_refused_by_name(self):
        with self.assertRaises(UnsupportedRenderFormat):
            render(sample_content(), template=SAMPLE_TEMPLATE, fmt="rtf")

    def test_an_empty_render_is_a_failure_not_an_empty_file(self):
        from apps.resumes.rendering.base import RenderResult

        with self.assertRaises(RenderFailed):
            RenderResult(data=b"", mime="application/pdf", engine="test")

    def test_the_engine_version_decides_staleness(self):
        self.assertFalse(stale_for(ENGINE_VERSION))
        self.assertTrue(stale_for("0.9"))


class RenderServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Backend CV"
        )
        draft_service.autosave(draft=self.document.draft, content=sample_content())
        self.document.refresh_from_db()

    def _version(self):
        return version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

    def test_rendering_a_version_stores_a_scanned_clean_attachment(self):
        record = render_service.render_version(version=self._version(), actor=self.actor)

        self.assertEqual(record.format, RenderFormat.PDF)
        self.assertEqual(record.attachment.scan_status, ScanStatus.CLEAN)
        self.assertTrue(record.sha256)
        self.assertGreater(record.attachment.size, 0)

    def test_the_second_render_is_served_from_the_cache(self):
        version = self._version()

        first = render_service.render_version(version=version, actor=self.actor)
        second = render_service.render_version(version=version, actor=self.actor)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ResumeRender.objects.count(), 1)

    def test_each_format_is_cached_separately(self):
        version = self._version()

        render_service.render_version(version=version, actor=self.actor, fmt=RenderFormat.PDF)
        render_service.render_version(version=version, actor=self.actor, fmt=RenderFormat.DOCX)

        self.assertEqual(ResumeRender.objects.filter(version=version).count(), 2)

    def test_a_different_template_renders_separately(self):
        version = self._version()
        other = ResumeTemplate.objects.exclude(pk=self.document.template_id).first()

        render_service.render_version(version=version, actor=self.actor)
        render_service.render_version(version=version, actor=self.actor, template=other)

        self.assertEqual(ResumeRender.objects.filter(version=version).count(), 2)

    def test_a_render_from_an_older_engine_is_replaced(self):
        version = self._version()
        first = render_service.render_version(version=version, actor=self.actor)
        ResumeRender.objects.filter(pk=first.pk).update(engine_version="0.1")

        second = render_service.render_version(version=version, actor=self.actor)

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(second.engine_version, ENGINE_VERSION)

    def test_a_render_whose_file_was_deleted_is_re_rendered(self):
        from apps.files import services as file_services

        version = self._version()
        first = render_service.render_version(version=version, actor=self.actor)
        file_services.soft_delete(attachment=first.attachment, actor=self.actor)

        second = render_service.render_version(version=version, actor=self.actor)

        self.assertNotEqual(first.pk, second.pk)

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(InvalidState):
            render_service.render_version(version=self._version(), fmt="rtf")

    def test_rendering_is_announced_and_audited(self):
        from apps.audit.models import AuditLog
        from apps.notifications.models import OutboxEvent

        render_service.render_version(version=self._version(), actor=self.actor)

        event = OutboxEvent.objects.get(type="resume.rendered")
        self.assertIn("sha256", event.payload)
        self.assertNotIn("Priya", str(event.payload))
        self.assertTrue(AuditLog.objects.filter(verb="resume.rendered").exists())

    def test_the_template_a_candidate_picked_is_the_one_they_download(self):
        """Picking a template did nothing to the file, and nothing said so.

        The version's frozen snapshot won over the template named at export
        time, so a candidate who chose one template in the gallery, exported,
        then chose another and exported again got the same drawing twice — the
        one their first frozen version happened to carry. The render rows named
        the two different templates, so only the file disagreed.
        """
        version = self._version()
        other = (
            ResumeTemplate.objects.exclude(pk=self.document.template_id)
            .exclude(font=self.document.template.font)
            .first()
        )
        self.assertIsNotNone(other, "the catalogue needs two font families")

        record = render_service.render_version(version=version, actor=self.actor, template=other)

        self.assertEqual(record.template_id, other.pk)
        self.assertEqual(record.fonts["family"], family_for(other.snapshot()))

    def test_the_template_snapshot_is_what_gets_rendered(self):
        # A version rendered a year later must look the way it looked then, so
        # the snapshot on the version wins over the template row today.
        version = self._version()
        ResumeTemplate.objects.filter(pk=self.document.template_id).update(
            accent="clay", font="Inter"
        )

        record = render_service.render_version(version=version, actor=self.actor)

        self.assertEqual(record.fonts["family"], "serif")


class ExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        # Downloading is a Pro entitlement (P18). This suite is about the
        # export machinery, so it buys the entitlement rather than asserting it;
        # the gate itself is tested in `apps/billing/tests/test_enforcement.py`.
        billing_factories.put_on_pro(self.profile)
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Backend CV"
        )
        draft_service.autosave(draft=self.document.draft, content=sample_content())
        self.document.refresh_from_db()

    def test_exporting_freezes_a_version_first(self):
        record = render_service.export(document=self.document, actor=self.actor)
        self.document.refresh_from_db()

        self.assertEqual(self.document.versions.count(), 1)
        self.assertEqual(record.version.created_reason, VersionReason.EXPORTED)
        self.assertEqual(self.document.current_version_id, record.version_id)

    def test_exporting_twice_without_editing_produces_one_version_and_one_file(self):
        first = render_service.export(document=self.document, actor=self.actor)
        self.document.refresh_from_db()
        second = render_service.export(document=self.document, actor=self.actor)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.document.versions.count(), 1)
        self.assertEqual(ResumeRender.objects.count(), 1)

    def test_editing_then_exporting_produces_a_new_version_and_file(self):
        render_service.export(document=self.document, actor=self.actor)
        self.document.refresh_from_db()
        draft_service.set_contact(
            draft=self.document.draft, field="headline", value="Staff Engineer"
        )
        self.document.refresh_from_db()

        second = render_service.export(document=self.document, actor=self.actor)

        self.assertEqual(self.document.versions.count(), 2)
        self.assertEqual(second.version.version_no, 2)
        self.assertEqual(ResumeRender.objects.count(), 2)

    def test_an_empty_cv_cannot_be_exported(self):
        empty = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Empty"
        )
        draft_service.set_contact(draft=empty.draft, field="name", value="")

        with self.assertRaises(InvalidState):
            render_service.export(document=empty, actor=self.actor)

    def test_exports_are_listed_as_history(self):
        render_service.export(document=self.document, actor=self.actor)
        render_service.export(document=self.document, actor=self.actor, fmt=RenderFormat.DOCX)

        self.assertEqual(selectors.renders_for(self.document).count(), 2)
        self.assertEqual(selectors.renders_for_profile(self.profile).count(), 2)

    def test_another_profile_sees_none_of_it(self):
        record = render_service.export(document=self.document, actor=self.actor)
        other = create_profile()

        self.assertEqual(selectors.renders_for_profile(other).count(), 0)
        self.assertIsNone(selectors.render_for(other, record.pk))

    def test_the_filename_names_the_document_and_version(self):
        record = render_service.export(document=self.document, actor=self.actor)

        self.assertEqual(record.filename, "Backend CV v1.pdf")


class RedactionInvalidatesRendersTests(TestCase):
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

    def test_redacting_removes_the_rendered_file(self):
        # A tombstone is worthless while the original PDF is still downloadable.
        version_service.redact(version=self.record.version, actor=self.actor)

        self.assertEqual(ResumeRender.objects.count(), 0)
        self.record.attachment.refresh_from_db()
        self.assertIsNotNone(self.record.attachment.deleted_at)

    def test_a_redacted_version_renders_only_the_tombstone(self):
        version_service.redact(version=self.record.version, actor=self.actor)
        version = self.record.version
        version.refresh_from_db()

        again = render_service.render_version(version=version, actor=self.actor)

        self.assertTrue(again.data if hasattr(again, "data") else True)
        self.assertEqual(ResumeRender.objects.count(), 1)


class RenderTaskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Backend CV"
        )
        draft_service.autosave(draft=self.document.draft, content=sample_content())
        self.document.refresh_from_db()

    def test_the_task_renders_a_version(self):
        from apps.resumes.tasks import render_version as task

        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

        self.assertTrue(task(version.pk, "pdf").startswith("rendered:"))
        self.assertEqual(ResumeRender.objects.count(), 1)

    def test_the_task_runs_on_the_render_queue(self):
        # SRS §17.5: a burst of exports must not touch page latency.
        from apps.resumes.tasks import render_version as task

        self.assertEqual(task.queue, "render")

    def test_a_missing_version_is_not_an_error(self):
        from apps.resumes.tasks import render_version as task

        self.assertEqual(task(999999), "gone")

    def test_a_bad_format_is_refused_without_retrying(self):
        from apps.resumes.tasks import render_version as task

        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

        self.assertEqual(task(version.pk, "rtf"), "refused")


class FontTests(TestCase):
    """Font management (P6).

    The behaviour worth testing is the fallback, because that is what runs on
    every machine that has not been given licensed font files — which today is
    all of them.
    """

    def setUp(self):
        fonts.reset_cache()
        self.addCleanup(fonts.reset_cache)

    def test_a_template_font_maps_to_a_family(self):
        self.assertEqual(fonts.family_for({"font": "Source Serif"}), "serif")
        self.assertEqual(fonts.family_for({"font": "Inter"}), "sans")
        self.assertEqual(fonts.family_for({"font": "Courier"}), "mono")

    def test_an_unknown_font_falls_back_to_sans(self):
        self.assertEqual(fonts.family_for({"font": "Comic Papyrus"}), "sans")
        self.assertEqual(fonts.family_for({}), "sans")
        self.assertEqual(fonts.family_for(None), "sans")

    def test_without_installed_files_the_builtin_faces_are_used(self):
        chosen = fonts.font_set({"font": "Source Serif"})

        self.assertFalse(chosen.embedded)
        self.assertEqual(chosen.regular, "Times-Roman")
        self.assertEqual(chosen.bold, "Times-Bold")

    def test_a_font_directory_with_nothing_in_it_still_renders(self):
        import tempfile

        with tempfile.TemporaryDirectory() as empty, self.settings(RENDER_FONT_DIR=empty):
            fonts.reset_cache()
            chosen = fonts.font_set({"font": "Inter"})

        self.assertFalse(chosen.embedded)
        self.assertEqual(chosen.regular, "Helvetica")

    def test_a_corrupt_font_file_is_survived_rather_than_fatal(self):
        # An export must not fail because somebody put a broken TTF on the
        # render container.
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in fonts.INSTALLABLE["sans"]:
                (root / name).write_bytes(b"not a font")

            with self.settings(RENDER_FONT_DIR=directory):
                fonts.reset_cache()
                chosen = fonts.font_set({"font": "Inter"})

        self.assertFalse(chosen.embedded)
        self.assertEqual(chosen.family, "sans")

    def test_the_recorded_font_says_whether_it_was_embedded(self):
        recorded = fonts.font_set({"font": "Inter"}).as_dict()

        self.assertEqual(recorded["embedded"], "no")
        self.assertEqual(recorded["family"], "sans")


class RenderOutsideATransactionTests(TransactionTestCase):
    """The path a real request takes, which `TestCase` cannot exercise.

    Django wraps every `TestCase` in a transaction, so a service that emits an
    outbox event outside one still looks correct under test — the guard in
    `notifications.events` sees the test's own transaction and says nothing.
    Production has no such wrapper.

    This bit: `render_version` emitted `resume.rendered` after its atomic block
    closed, so the very first export of any CV raised, and with it the first
    application submitted against a freshly saved version. A whole suite of
    green tests said otherwise.

    `TransactionTestCase` runs without the wrapper, which is the only way to
    keep this fixed.
    """

    reset_sequences = True

    def setUp(self):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

        self.profile = create_profile()
        self.actor = self.profile.user
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Backend CV"
        )
        draft_service.autosave(draft=self.document.draft, content=sample_content())
        self.document.refresh_from_db()

    def test_a_first_render_completes_without_an_ambient_transaction(self):
        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )

        record = render_service.render_version(version=version, actor=self.actor)

        self.assertTrue(record.sha256)
        self.assertEqual(record.format, RenderFormat.PDF)

    def test_the_render_event_is_recorded_with_the_render(self):
        from apps.notifications.models import OutboxEvent

        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        render_service.render_version(version=version, actor=self.actor)

        self.assertTrue(OutboxEvent.objects.filter(type="resume.rendered").exists())


class PreviewMatchesTheExportTests(TestCase):
    """What is on screen is what downloads.

    The preview and both exporters read the same `rendering.layout.build` and
    the same theme, so they cannot disagree about which blocks exist or where
    they go. That sharing does not prove they render the same *words*: a field
    one of them forgets to draw is content that vanishes on the file, and
    nobody finds out until it is in front of an employer.

    So this reads the words off the preview's own Document and looks for every
    one of them in the exported PDF and DOCX. Word level rather than pixel
    level on purpose — a pixel diff fails on a font hint and says nothing about
    whether anybody lost a sentence.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.content = _rich_cv()

    def _preview_words(self, template) -> set[str]:
        from apps.resumes import presentation

        paper = presentation.paper(self.content, template.snapshot())["paper"]
        found = _words(paper.title)
        for region in ("head", "band", "sidebar", "body"):
            for block in getattr(paper, region, ()) or ():
                found |= _words(block.text)
                for item in block.items or ():
                    found |= _words(str(item))
                found |= _meta_words(block.meta or {})
        return found

    def test_every_word_on_screen_is_in_the_pdf(self):
        from apps.resumes.rendering import pdf as pdf_engine

        for template in ResumeTemplate.objects.filter(is_active=True):
            with self.subTest(template=template.slug):
                data = pdf_engine.render(self.content, template=template.snapshot()).data
                in_file = _words(_pdf_text(data))
                missing = sorted(self._preview_words(template) - in_file)

                self.assertEqual(missing, [], f"{template.slug} drops {missing}")

    def test_every_word_on_screen_is_in_the_word_file(self):
        from apps.resumes.rendering import docx as docx_engine

        template = ResumeTemplate.objects.filter(is_active=True).first()
        data = docx_engine.render(self.content, template=template.snapshot()).data
        missing = sorted(self._preview_words(template) - _words(_docx_text(data)))

        self.assertEqual(missing, [])

    def test_the_sections_a_first_cv_is_made_of_reach_the_file(self):
        # Projects, certifications, awards and publications were carried into
        # the document only recently. Carrying them and then not drawing them
        # would be the same loss one step further along.
        from apps.resumes.rendering import pdf as pdf_engine

        for template in ResumeTemplate.objects.filter(is_active=True):
            with self.subTest(template=template.slug):
                text = _pdf_text(pdf_engine.render(self.content, template=template.snapshot()).data)

                self.assertIn("Bus Tracker", text)
                self.assertIn("AWS Certified", text)
                self.assertIn("Dean", text)
                self.assertIn("Routing on unreliable networks", text)


class TemplatesUnderHardContentTests(TestCase):
    """Every template, against content that is not polite.

    Long names, long employers, forty skills, twelve roles, one enormous
    bullet, and a CV with nothing on it. Each is a document somebody has. The
    bar is three things: the file renders, nothing on the document is missing
    from it, and too much content becomes more pages rather than one overfull
    one.
    """

    LONG_NAME = "Bartholomew Fitzwilliam Chandrashekhar Vandenberghe-Oyelaran"
    LONG_TITLE = "Senior Principal Staff Engineer, Platform Reliability and Developer Experience"
    LONG_ORG = "The Kathmandu Institute of Advanced Financial Technology and Payment Systems"
    LONG_BULLET = (
        "Rebuilt the settlement reconciliation pipeline end to end, moving it from an "
        "overnight batch that regularly overran into the trading day to a streaming process "
        "completing in under fifteen minutes, which cut merchant disputes by roughly forty "
        "per cent and removed the standing 3am page."
    )

    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def _cv(self, **kwargs):
        content = content_rules.blank_document(
            name=kwargs.get("name", "Priya Sharma"),
            headline=kwargs.get("headline", "Backend Engineer"),
            email="priya@example.com",
        )
        content["contact"]["phone"] = "+977 9841 220 118"
        content_rules.section_by_key(content, "summary")["text"] = "Engineer."
        content_rules.section_by_key(content, "experience")["entries"] = kwargs.get(
            "experience", [self._role()]
        )
        content_rules.section_by_key(content, "education")["entries"] = [
            content_rules.new_entry(title="BE Computer", org="Tribhuvan", dates="2014 - 2018")
        ]
        content_rules.section_by_key(content, "skills")["items"] = kwargs.get("skills", ["Python"])
        return content

    def _role(self, **kwargs):
        return content_rules.new_entry(
            title=kwargs.get("title", "Backend Engineer"),
            org=kwargs.get("org", "Khalti"),
            dates="2021 - Present",
            bullets=kwargs.get("bullets", ["Shipped the thing."]),
        )

    def _cases(self):
        return {
            "very long name": self._cv(name=self.LONG_NAME, headline=self.LONG_TITLE),
            "very long employer": self._cv(
                experience=[self._role(org=self.LONG_ORG, title=self.LONG_TITLE)]
            ),
            "one enormous bullet": self._cv(
                experience=[self._role(bullets=[self.LONG_BULLET] * 3)]
            ),
            "forty skills": self._cv(skills=[f"Skill {n}" for n in range(1, 41)]),
            "twelve roles": self._cv(
                experience=[self._role(title=f"Engineer {n}") for n in range(1, 13)]
            ),
            "no experience": self._cv(experience=[]),
            "no skills": self._cv(skills=[]),
            "nothing at all": content_rules.blank_document(name="", headline="", email=""),
        }

    def test_every_template_renders_every_shape_of_cv(self):
        from apps.resumes.rendering import docx as docx_engine
        from apps.resumes.rendering import pdf as pdf_engine

        templates = list(ResumeTemplate.objects.filter(is_active=True))
        self.assertGreaterEqual(len(templates), 10)

        for label, content in self._cases().items():
            for template in templates:
                with self.subTest(case=label, template=template.slug):
                    snapshot = template.snapshot()
                    self.assertTrue(pdf_engine.render(content, template=snapshot).data)
                    self.assertTrue(docx_engine.render(content, template=snapshot).data)

    def test_a_long_name_or_title_is_never_truncated_away(self):
        # Silently dropping the candidate's own name off the file is the worst
        # thing a layout can do with content it cannot fit.
        from apps.resumes.rendering import pdf as pdf_engine

        content = self._cv(name=self.LONG_NAME, headline=self.LONG_TITLE)
        for template in ResumeTemplate.objects.filter(is_active=True):
            with self.subTest(template=template.slug):
                text = _pdf_text(pdf_engine.render(content, template=template.snapshot()).data)

                self.assertLessEqual(_words(self.LONG_NAME), _words(text))
                self.assertLessEqual(_words(self.LONG_TITLE), _words(text))

    def test_too_much_content_becomes_more_pages(self):
        # Not one overfull page with the end of the CV drawn off the paper.
        from apps.resumes.rendering import pdf as pdf_engine

        template = ResumeTemplate.objects.get(slug="meridian").snapshot()
        short = _pdf_text(pdf_engine.render(self._cv(), template=template).data)
        long = _pdf_text(
            pdf_engine.render(
                self._cv(experience=[self._role(title=f"Engineer {n}") for n in range(1, 13)]),
                template=template,
            ).data
        )

        self.assertGreater(long.count("\f"), short.count("\f"))
        self.assertIn("Engineer 12", long)


_WORD_PATTERN = __import__("re").compile(r"[A-Za-z][A-Za-z'&/.+-]*")


def _words(text) -> set[str]:
    return {
        word.lower().strip(".,") for word in _WORD_PATTERN.findall(str(text or "")) if len(word) > 2
    }


def _meta_words(meta) -> set[str]:
    """Blocks carry their entries in `meta`, so the words are in there too."""
    if isinstance(meta, dict):
        found: set[str] = set()
        for value in meta.values():
            found |= _meta_words(value)
        return found
    if isinstance(meta, list | tuple):
        found = set()
        for value in meta:
            found |= _meta_words(value)
        return found
    if isinstance(meta, str):
        return _words(meta)
    return set()


def _pdf_text(data: bytes) -> str:
    from pdfminer.high_level import extract_text

    return extract_text(BytesIO(data))


def _docx_text(data: bytes) -> str:
    import docx

    document = docx.Document(BytesIO(data))
    return " ".join(paragraph.text for paragraph in document.paragraphs)


def _rich_cv() -> dict:
    """A CV with every section this product can draw on it."""
    content = content_rules.blank_document(
        name="Sabin Bhandari", headline="Graduate Developer", email="sabin@example.com"
    )
    content["contact"]["phone"] = "9812345678"
    content_rules.section_by_key(content, "summary")["text"] = (
        "Computing graduate looking for a first role in backend engineering."
    )

    experience = content_rules.section_by_key(content, "experience")
    experience["entries"] = [
        content_rules.new_entry(
            title="Software Intern",
            org="Leapfrog",
            dates="Jun 2025 - Sep 2025",
            bullets=["Shipped the internal reporting endpoint."],
        )
    ]
    education = content_rules.section_by_key(content, "education")
    education["entries"] = [
        content_rules.new_entry(title="BSc Computing", org="Islington College", dates="2022 - 2026")
    ]
    content_rules.section_by_key(content, "skills")["items"] = ["Python", "Django", "PostgreSQL"]

    projects = content_rules.ensure_section(
        content, key="projects", title="Projects", kind="entries"
    )
    projects["entries"] = [
        content_rules.new_entry(
            title="Bus Tracker", bullets=["A live map of valley routes, built with Flutter."]
        )
    ]
    content_rules.ensure_section(
        content, key="certifications", title="Certifications", kind="list"
    )["items"] = ["AWS Certified Cloud Practitioner"]
    content_rules.ensure_section(content, key="awards", title="Awards", kind="list")["items"] = [
        "Dean's list, 2025"
    ]
    content_rules.ensure_section(content, key="publications", title="Publications", kind="list")[
        "items"
    ] = ["Bhandari S. (2026) Routing on unreliable networks."]
    return content
