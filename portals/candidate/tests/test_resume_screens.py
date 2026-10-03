"""
Resume screens on real records (P5).

The screens are thin, so these tests concentrate on the two things a thin view
can still get wrong: rendering someone else's CV, and reporting a save that did
not happen.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.billing.tests import factories as billing_factories
from apps.candidates.services import profile as profile_service
from apps.resumes import content as content_rules
from apps.resumes.models import BuilderMode, ResumeDocument, ResumeVersion
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from portals.candidate import resume_views


class ResumeScreenFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.user = create_user(full_name="Priya Sharma")
        self.client.force_login(self.user)
        self.profile = profile_service.get_or_create_profile(user=self.user)
        self.document = document_service.create_document(
            profile=self.profile, actor=self.user, name="Backend CV"
        )
        self.act = reverse("resume:builder_act", kwargs={"document_id": self.document.public_id})


class SignedOutTests(TestCase):
    def test_the_builder_requires_signing_in(self):
        for name in ("resume:builder", "resume:upload", "resume:templates", "resume:analysis"):
            with self.subTest(url=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)

    def test_guest_upload_does_not(self):
        # The whole point: someone with no account can start here (ADR-A32).
        response = self.client.get(reverse("resume:guest_upload"))

        self.assertEqual(response.status_code, 200)


class BuilderHubTests(ResumeScreenFixture):
    def test_the_hub_lists_the_candidates_cvs(self):
        response = self.client.get(reverse("resume:builder"))

        self.assertContains(response, "Backend CV")
        self.assertContains(response, "Main CV")

    def test_starting_a_cv_creates_a_document_and_opens_it(self):
        response = self.client.post(
            reverse("resume:builder"),
            {"name": "Data roles", "mode": BuilderMode.GUIDED, "template": "atlas"},
        )

        document = ResumeDocument.objects.get(name="Data roles")
        self.assertEqual(document.builder_mode, BuilderMode.GUIDED)
        self.assertRedirects(
            response,
            reverse("resume:guided", kwargs={"document_id": document.public_id}),
            fetch_redirect_response=False,
        )

    def test_document_actions_run_through_the_service(self):
        second = document_service.create_document(
            profile=self.profile, actor=self.user, name="Second"
        )

        self.client.post(
            reverse("resume:document_act"),
            {"command": "make_primary", "document": str(second.public_id)},
        )
        second.refresh_from_db()
        self.document.refresh_from_db()

        self.assertTrue(second.is_primary)
        self.assertFalse(self.document.is_primary)

    def test_a_refused_action_is_reported_rather_than_silent(self):
        response = self.client.post(
            reverse("resume:document_act"),
            {"command": "archive", "document": str(self.document.public_id)},
        )

        # RES-03: the refusal keeps its real status and the header htmx swaps on.
        self.assertEqual(response["X-Restriction"], "state")
        self.assertContains(response, "Make another CV primary", status_code=409)


class EditorScreenTests(ResumeScreenFixture):
    def test_the_editor_renders_the_document(self):
        response = self.client.get(
            reverse("resume:editor", kwargs={"document_id": self.document.public_id})
        )

        self.assertContains(response, "Priya Sharma")
        self.assertContains(response, "Save a version")

    def test_opening_the_editor_without_an_id_falls_back_to_the_main_cv(self):
        response = self.client.get(reverse("resume:editor_default"))

        self.assertContains(response, "Backend CV")

    def test_a_candidate_with_no_cv_is_sent_to_the_hub(self):
        document_service.delete(document=self.document, actor=self.user)

        response = self.client.get(reverse("resume:editor_default"))

        self.assertRedirects(response, reverse("resume:builder"))

    def test_editing_a_contact_field_writes_the_draft(self):
        response = self.client.post(
            self.act, {"command": "contact", "field": "headline", "value": "Backend Engineer"}
        )
        self.document.refresh_from_db()

        self.assertEqual(self.document.draft.content["contact"]["headline"], "Backend Engineer")
        self.assertContains(response, "Backend Engineer")

    def test_a_rejected_edit_says_why(self):
        response = self.client.post(
            self.act, {"command": "contact", "field": "blood_type", "value": "O"}
        )

        self.assertContains(response, "not a contact field", status_code=409)

    def test_saving_a_version_from_the_editor(self):
        draft_service.set_contact(draft=self.document.draft, field="name", value="Priya Sharma")

        response = self.client.post(self.act, {"command": "save_version"})
        self.document.refresh_from_db()

        self.assertEqual(self.document.versions.count(), 1)
        self.assertIn("Saved version 1", response["HX-Trigger"])

    def test_the_history_says_which_version_is_which(self):
        """Two saves used to be two timestamps and nothing else.

        The note a candidate types when they save has been stored since the
        first version of this feature; nothing ever sent one and nothing ever
        showed one, so the history was a column of identical rows and the only
        way to tell them apart was to restore one and look at it.
        """
        draft_service.set_contact(draft=self.document.draft, field="name", value="Priya Sharma")
        self.client.post(self.act, {"command": "save_version", "note": "Before trimming"})
        draft_service.add_section(draft=self.document.draft, title="Volunteering", kind="list")
        self.client.post(self.act, {"command": "save_version", "note": "Cut for Fonepay"})

        response = self.client.get(
            reverse("resume:editor", kwargs={"document_id": self.document.public_id})
        )

        self.assertContains(response, "Before trimming")
        self.assertContains(response, "Cut for Fonepay")
        # And a count of what is in each, so the two rows differ on more than
        # a minute of clock time.
        self.assertContains(response, "sections")
        self.assertContains(response, "Restoring replaces what is in the editor now")

    def test_autosave_does_not_create_versions_through_the_screen(self):
        import json as json_module

        content = dict(self.document.draft.content)
        content["summary"] = "Typed while thinking."

        for _ in range(5):
            self.client.post(
                self.act, {"command": "autosave", "content": json_module.dumps(content)}
            )

        self.assertEqual(self.document.versions.count(), 0)

    def test_an_unknown_command_is_a_bad_request(self):
        response = self.client.post(self.act, {"command": "drop_tables"})

        self.assertEqual(response.status_code, 400)

    def test_one_candidate_cannot_edit_another_candidates_cv(self):
        other = create_user()
        self.client.force_login(other)

        response = self.client.post(
            self.act, {"command": "contact", "field": "name", "value": "Theirs"}
        )
        self.document.refresh_from_db()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.document.draft.content["contact"]["name"], "Priya Sharma")

    def test_one_candidate_cannot_open_another_candidates_cv(self):
        other = create_user()
        self.client.force_login(other)

        response = self.client.get(
            reverse("resume:editor", kwargs={"document_id": self.document.public_id})
        )

        self.assertRedirects(response, reverse("resume:builder"))

    def test_the_editor_offers_a_control_for_every_thing_a_cv_needs(self):
        """A CV builder has to let somebody type their CV into it.

        The commands all existed on `BuilderCommandView`; the screen exposed
        move-up, move-down and hide, and nothing at all for the words. So this
        asserts the controls rather than the handlers.
        """
        experience = content_rules.section_by_key(self.document.draft.content, "experience")
        draft_service.add_entry(draft=self.document.draft, section_id=experience["id"])

        response = self.client.get(
            reverse("resume:editor", kwargs={"document_id": self.document.public_id})
        )
        body = response.content.decode()

        for command in (
            "rename_section",
            "delete_section",
            "add_section",
            "section_text",
            "section_items",
            "add_entry",
            "update_entry",
            "delete_entry",
            "bullets",
        ):
            self.assertIn(command, body, f"the editor offers no control for {command}")

        # Reordering by drag names its command in the script rather than the
        # markup, so the markup is asserted through the hooks the script binds
        # to, and the handler through the view.
        self.assertIn("data-sortable", body)
        self.assertIn("data-sortable-handle", body)
        self.assertTrue(hasattr(resume_views.BuilderCommandView, "do_reorder_sections"))

    def test_the_print_view_renders_the_document_alone(self):
        response = self.client.get(
            reverse("resume:print", kwargs={"document_id": self.document.public_id})
        )

        # The sheet, and the theme the exporters will use for the same
        # document — the point of the print route is that it is not a
        # differently-styled copy of the CV.
        self.assertContains(response, "sheet-inner")
        self.assertContains(response, "--sheet-accent")


class GuidedAndAssistantTests(ResumeScreenFixture):
    def test_the_guided_screen_reports_real_progress(self):
        response = self.client.get(
            reverse("resume:guided", kwargs={"document_id": self.document.public_id})
        )

        self.assertContains(response, "of 6 done")

    def test_a_step_that_does_not_apply_can_be_set_aside(self):
        """The flow used to be five required steps and one of them was a job.

        A student, a graduate or a school leaver could not reach the end of it
        without inventing employment, and the counter sat at "4 of 5" at them
        until they did.
        """
        self.client.post(self.act, {"command": "step_aside", "step": "experience"})

        response = self.client.get(
            reverse("resume:guided", kwargs={"document_id": self.document.public_id})
        )

        self.assertContains(response, "of 5 done")
        self.assertContains(response, "1 set aside as not applying to you")

    def test_setting_a_step_aside_writes_nothing_to_the_cv(self):
        before = self.document.draft.content

        self.client.post(self.act, {"command": "step_aside", "step": "experience"})

        self.document.draft.refresh_from_db()
        self.assertEqual(self.document.draft.content, before)

    def test_a_step_every_cv_needs_cannot_be_set_aside(self):
        response = self.client.post(self.act, {"command": "step_aside", "step": "contact"})

        self.assertEqual(
            self.document.builder_sessions.filter(mode=BuilderMode.GUIDED)
            .values_list("step_state", flat=True)
            .first()
            or {},
            {},
        )
        self.assertLess(response.status_code, 500)

    def test_choosing_a_step_is_remembered(self):
        self.client.post(self.act, {"command": "step", "step": "skills"})

        session = self.document.builder_sessions.get(mode=BuilderMode.GUIDED)
        self.assertEqual(session.step_state["current"], "skills")

    def test_the_assistant_answers_a_turn_without_touching_the_draft(self):
        """P5 recorded the turn; P14 answers it. Neither writes the draft.

        The transcript now holds both sides. The property that matters is
        unchanged and is asserted more strongly than before: the assistant
        speaks, and the document is byte-identical afterwards (INV-3).

        No provider is configured here, so the reply is the "could not draft"
        message — which is the point. A candidate on a deployment with no AI
        gets a sentence, not a broken screen.
        """
        from apps.ai.services import prompts

        prompts.load_all()
        before = dict(self.document.draft.content)

        # The answer is worked on in the background (`services.improvements`);
        # running the queued work here is what a worker does.
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.act, {"command": "turn", "text": "I worked at Nexa."})
        self.document.refresh_from_db()

        session = self.document.builder_sessions.get(mode=BuilderMode.ASSISTANT)
        self.assertEqual([turn["role"] for turn in session.transcript], ["candidate", "assistant"])
        self.assertIn("Nexa", session.transcript[0]["text"])
        self.assertEqual(self.document.draft.content, before)

    def test_opening_a_mode_records_it_on_the_document(self):
        self.client.get(reverse("resume:ai"))
        self.document.refresh_from_db()

        self.assertEqual(self.document.builder_mode, BuilderMode.ASSISTANT)


class TemplateScreenTests(ResumeScreenFixture):
    def test_the_gallery_lists_active_templates(self):
        response = self.client.get(reverse("resume:templates"))

        self.assertContains(response, "Meridian")
        self.assertContains(response, "Atlas")

    def test_the_gallery_filters(self):
        response = self.client.get(reverse("resume:templates"), {"layout": "sidebar"})

        self.assertContains(response, "Atlas")
        self.assertNotContains(response, "Plainspoke")

    def test_favourites_are_remembered_for_the_browser(self):
        self.client.post(reverse("resume:template_act"), {"command": "favourite", "slug": "atlas"})

        response = self.client.get(reverse("resume:templates"))
        self.assertContains(response, "text-brand-600")

    def test_comparison_is_capped(self):
        for slug in ("meridian", "atlas", "vertex"):
            self.client.post(reverse("resume:template_act"), {"command": "compare", "slug": slug})

        response = self.client.post(
            reverse("resume:template_act"), {"command": "compare", "slug": "plainspoke"}
        )

        self.assertContains(response, "up to 3 templates")

    def test_applying_a_template_changes_the_document_not_the_content(self):
        before = dict(self.document.draft.content)

        self.client.post(
            reverse("resume:template_act"),
            {"command": "apply", "slug": "atlas", "document": str(self.document.public_id)},
        )
        self.document.refresh_from_db()

        self.assertEqual(self.document.template.slug, "atlas")
        self.assertEqual(self.document.draft.content, before)

    def test_a_template_detail_page_renders(self):
        response = self.client.get(reverse("resume:template_detail", kwargs={"slug": "meridian"}))

        self.assertContains(response, "Meridian")


class GuestUploadScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def test_a_guest_upload_is_stored_and_the_token_kept_for_the_browser(self):
        from apps.resumes.models import ParseStatus, ResumeUpload
        from apps.resumes.tests.factories import uploaded_pdf
        from portals.candidate.resume_views import GUEST_TOKENS_KEY

        response = self.client.post(reverse("resume:guest_upload"), {"file": uploaded_pdf()})

        upload = ResumeUpload.objects.get()
        self.assertEqual(upload.parse_status, ParseStatus.BLOCKED)
        self.assertIsNone(upload.profile_id)
        self.assertEqual(len(self.client.session[GUEST_TOKENS_KEY]), 1)
        self.assertContains(response, "Create an account")

    def test_registering_then_claiming_attaches_the_upload(self):
        from apps.resumes.models import ParseStatus, ResumeUpload
        from apps.resumes.tests.factories import uploaded_pdf
        from portals.candidate.resume_views import claim_guest_uploads

        self.client.post(reverse("resume:guest_upload"), {"file": uploaded_pdf()})

        user = create_user()
        self.client.force_login(user)
        request = _request_with_session(self.client, user)

        claimed = claim_guest_uploads(request)

        upload = ResumeUpload.objects.get()
        self.assertEqual(claimed, 1)
        self.assertIsNotNone(upload.profile_id)
        self.assertEqual(upload.parse_status, ParseStatus.PENDING)

    def test_a_bad_file_is_refused_with_a_reason(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            reverse("resume:guest_upload"),
            {"file": SimpleUploadedFile("cv.png", b"\x89PNG" + b"0" * 64, "image/png")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response, "Upload your CV or resume in PDF, DOC, or DOCX format.", status_code=400
        )
        self.assertContains(response, "That file is an image", status_code=400)


class UploadScreenTests(ResumeScreenFixture):
    def test_uploading_shows_it_in_the_history(self):
        from apps.resumes.tests.factories import uploaded_pdf

        response = self.client.post(reverse("resume:upload_receive"), {"file": uploaded_pdf()})

        self.assertContains(response, "cv.pdf")

    def test_removing_an_uploaded_cv_asks_first_and_says_what_goes(self):
        """It used to happen on one click, with nothing asked.

        Removing an upload deletes the file the candidate gave us and the
        reading of it, and there is no undo on this screen the way there is in
        the editor. The sentence answers the question somebody actually has
        before they press it, which is not "are you sure" but "what else goes
        with it".
        """
        from apps.resumes.tests.factories import uploaded_pdf

        self.client.post(reverse("resume:upload_receive"), {"file": uploaded_pdf()})

        response = self.client.get(reverse("resume:upload"))

        self.assertContains(response, "hx-confirm")
        self.assertContains(response, "cannot be undone")
        self.assertContains(response, "stays too")
        self.assertContains(response, 'data-confirm-tone="danger"')

    def test_uploading_nothing_says_so(self):
        response = self.client.post(reverse("resume:upload_receive"), {})

        self.assertContains(response, "Choose a file first")

    def test_building_a_document_from_a_parsed_upload(self):
        from django.utils import timezone

        from apps.files.constants import ScanStatus
        from apps.files.models import Attachment
        from apps.resumes.services import parse as parse_service
        from apps.resumes.services import upload as upload_service
        from apps.resumes.tests.factories import SAMPLE_CV, uploaded_docx

        upload, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.user, uploaded_file=uploaded_docx(text=SAMPLE_CV)
        )
        Attachment.objects.filter(pk=upload.attachment_id).update(
            scan_status=ScanStatus.CLEAN, scanned_at=timezone.now()
        )
        upload.attachment.refresh_from_db()
        parse_service.parse_upload(upload=upload)

        response = self.client.post(
            reverse("resume:import_act"),
            {"command": "build", "upload": str(upload.public_id)},
        )

        document = ResumeDocument.objects.get(upload=upload)
        self.assertRedirects(
            response,
            reverse("resume:editor", kwargs={"document_id": document.public_id}),
            fetch_redirect_response=False,
        )
        experience = content_rules.section_by_key(document.draft.content, "experience")
        self.assertTrue(experience["entries"])


class AnalysisScreenTests(ResumeScreenFixture):
    """`/cv/analysis/` after P12.

    The screen showed version history while there was nothing else it could
    honestly say. P12 gave it a report, and the version list stayed where it is
    actually used — beside the editor, in `resume/_versions.html`. This asserts
    both, so neither the report nor the history can quietly disappear.
    """

    def test_the_screen_offers_an_analysis_for_a_saved_cv(self):
        draft_service.set_contact(draft=self.document.draft, field="name", value="Priya Sharma")
        self.client.post(self.act, {"command": "save_version"})

        response = self.client.get(reverse("resume:analysis"))

        self.assertContains(response, "Analyse this CV")
        self.assertContains(response, "has not been analysed yet")
        self.assertEqual(ResumeVersion.objects.count(), 1)

    def test_version_history_is_still_reachable_from_the_editor(self):
        draft_service.set_contact(draft=self.document.draft, field="name", value="Priya Sharma")
        self.client.post(self.act, {"command": "save_version"})

        response = self.client.get(
            reverse("resume:editor", kwargs={"document_id": self.document.public_id})
        )
        self.assertContains(response, "Version 1")


def _request_with_session(client, user):
    """A request carrying this client's session, for helpers that read it."""
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    return request


class ExportScreenTests(ResumeScreenFixture):
    """Export, download and the authorization on both (P6)."""

    def setUp(self):
        super().setUp()
        # Downloading is a Pro entitlement (P18). This suite is about the export
        # screen, so it buys the entitlement; the gate is asserted in
        # `apps/billing/tests/test_enforcement.py`.
        billing_factories.put_on_pro(self.profile)
        draft_service.set_contact(draft=self.document.draft, field="name", value="Priya Sharma")
        self.document.refresh_from_db()
        self.export_url = reverse("resume:export", kwargs={"document_id": self.document.public_id})

    def test_exporting_produces_a_downloadable_file(self):
        from apps.resumes.models import ResumeRender

        response = self.client.post(self.export_url, {"format": "pdf"})

        record = ResumeRender.objects.get()
        self.assertContains(response, "Download")
        self.assertEqual(record.version.document_id, self.document.pk)
        self.assertIn("PDF ready", response["HX-Trigger"])

    def test_exporting_freezes_a_version_through_the_screen(self):
        self.client.post(self.export_url, {"format": "pdf"})
        self.document.refresh_from_db()

        self.assertEqual(self.document.versions.count(), 1)
        self.assertEqual(self.document.current_version.created_reason, "exported")

    def test_exporting_word_works_too(self):
        from apps.resumes.models import RenderFormat, ResumeRender

        self.client.post(self.export_url, {"format": "docx"})

        self.assertEqual(ResumeRender.objects.get().format, RenderFormat.DOCX)

    def test_an_empty_cv_reports_why_it_cannot_be_exported(self):
        empty = document_service.create_document(
            profile=self.profile, actor=self.user, name="Empty"
        )
        draft_service.set_contact(draft=empty.draft, field="name", value="")

        response = self.client.post(
            reverse("resume:export", kwargs={"document_id": empty.public_id}), {"format": "pdf"}
        )

        self.assertContains(response, "nothing in this CV", status_code=409)

    def test_downloading_redirects_to_a_signed_single_use_link(self):
        from apps.resumes.models import ResumeRender

        self.client.post(self.export_url, {"format": "pdf"})
        record = ResumeRender.objects.get()

        response = self.client.get(reverse("resume:download", kwargs={"render_id": record.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertIn(str(record.attachment.public_id), response["Location"])
        self.assertIn("?t=", response["Location"])

    def test_the_signed_link_actually_serves_the_file(self):
        from apps.resumes.models import ResumeRender

        self.client.post(self.export_url, {"format": "pdf"})
        record = ResumeRender.objects.get()

        redirected = self.client.get(reverse("resume:download", kwargs={"render_id": record.pk}))
        response = self.client.get(redirected["Location"])

        self.assertEqual(response.status_code, 200)

        # Held before the body is consumed, and closed directly afterwards.
        # `response.close()` would fire `request_finished`, which closes this
        # test's database connection and breaks whichever test runs next; a
        # leaked handle would keep the temporary media directory locked on
        # Windows. Neither is the file's fault, and both are avoidable here.
        stream = response.file_to_stream

        body = b"".join(response.streaming_content)
        self.assertTrue(body.startswith(b"%PDF"))
        self.assertIn("attachment", response["Content-Disposition"])

        if stream is not None:
            stream.close()

    def test_another_candidate_cannot_download_it(self):
        from apps.resumes.models import ResumeRender

        self.client.post(self.export_url, {"format": "pdf"})
        record = ResumeRender.objects.get()

        self.client.force_login(create_user())
        response = self.client.get(reverse("resume:download", kwargs={"render_id": record.pk}))

        self.assertEqual(response.status_code, 404)

    def test_a_signed_out_visitor_cannot_download_it(self):
        from apps.resumes.models import ResumeRender

        self.client.post(self.export_url, {"format": "pdf"})
        record = ResumeRender.objects.get()

        self.client.logout()
        response = self.client.get(reverse("resume:download", kwargs={"render_id": record.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/", response["Location"])

    def test_another_candidate_cannot_use_a_stolen_link(self):
        # The file view checks the token *and* the policy, so a leaked URL is
        # not a leaked CV.
        from apps.resumes.models import ResumeRender

        self.client.post(self.export_url, {"format": "pdf"})
        record = ResumeRender.objects.get()
        redirected = self.client.get(reverse("resume:download", kwargs={"render_id": record.pk}))
        stolen = redirected["Location"]

        self.client.force_login(create_user())
        response = self.client.get(stolen)

        self.assertEqual(response.status_code, 404)

    def test_one_candidate_cannot_export_another_candidates_cv(self):
        self.client.force_login(create_user())

        response = self.client.post(self.export_url, {"format": "pdf"})

        self.assertEqual(response.status_code, 404)

    def test_the_export_history_screen_lists_only_your_own(self):
        from apps.resumes.models import ResumeRender

        self.client.post(self.export_url, {"format": "pdf"})
        mine = ResumeRender.objects.get()

        response = self.client.get(reverse("resume:exports"))
        self.assertContains(response, "Backend CV")

        self.client.force_login(create_user())
        response = self.client.get(reverse("resume:exports"))
        self.assertNotContains(response, "Backend CV")
        self.assertTrue(ResumeRender.objects.filter(pk=mine.pk).exists())
