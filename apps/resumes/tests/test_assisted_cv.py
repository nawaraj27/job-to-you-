"""
Assisted CV as the primary way to build a CV (docs/Assisted-CV.md).

  * The way in is one prominent prompt with examples; starting creates an
    unsaved draft and sends the description as the first turn.
  * A double-click or resubmitted form starts one CV, not two.
  * The candidate's saved material is used only when they tick the box.
  * On the conversation page the composer comes first, then review, then the
    optional analysis checklist.
  * The assistant does not ask the same thing twice in one reply.
  * Assisted replies are metered and a model failure is refunded (ADR-A59).
"""

from __future__ import annotations

from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.ai.models import AIRun, RunStatus
from apps.billing.services import entitlements
from apps.candidates.tests.factories import create_profile
from apps.resumes.models import BuilderMode, ImprovementJob, ResumeDocument
from apps.resumes.services import assistant as assistant_service
from apps.resumes.services import documents as document_service
from apps.resumes.services import evidence

DESCRIPTION = (
    "I want a CV for customer support roles. I spent two years answering billing "
    "questions at a telecom company and I train new starters."
)


class AssistedFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.client.force_login(self.profile.user)

    def start(self, **data):
        payload = {"text": DESCRIPTION, "token": "tok-1", **data}
        with self.captureOnCommitCallbacks(execute=False):
            return self.client.post(reverse("resume:assistant_start"), payload)


class StartScreenTests(AssistedFixture):
    def test_the_prompt_is_the_way_in(self):
        page = self.client.get(reverse("resume:ai"))
        self.assertContains(page, "Describe the CV you want")
        self.assertContains(page, 'name="text"')
        self.assertContains(page, "Start my CV")
        self.assertContains(page, 'data-fill="#assist-text"')
        # Secondary routes are there, below, smaller.
        self.assertContains(page, "Start from a template")

    def test_a_candidate_with_cvs_reaches_it_with_new(self):
        document_service.create_document(profile=self.profile, actor=self.profile.user)
        self.assertContains(
            self.client.get(reverse("resume:ai") + "?new=1"), "Describe the CV you want"
        )

    def test_starting_creates_a_draft_and_sends_the_first_turn(self):
        response = self.start(target="Customer support officer")

        document = ResumeDocument.objects.get(profile=self.profile)
        self.assertRedirects(
            response,
            reverse("resume:assistant", kwargs={"document_id": document.public_id}),
            fetch_redirect_response=False,
        )
        self.assertIsNone(document.saved_at)  # a draft until they save it
        self.assertEqual(document.builder_mode, BuilderMode.ASSISTANT)
        self.assertEqual(document.name, "CV for Customer support officer")
        job = ImprovementJob.objects.get(document=document)
        self.assertEqual(job.payload["text"], DESCRIPTION)
        page = self.client.get(response["Location"])
        self.assertContains(page, "two years answering billing questions")

    def test_a_double_submission_starts_one_cv(self):
        self.start()
        self.start()
        self.assertEqual(ResumeDocument.objects.filter(profile=self.profile).count(), 1)

    def test_too_little_to_go_on_is_refused_without_creating_anything(self):
        response = self.start(text="CV please")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Tell us a little more", status_code=400)
        self.assertFalse(ResumeDocument.objects.filter(profile=self.profile).exists())

    def test_saved_material_is_used_only_when_ticked(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.files.models import Attachment
        from apps.resumes.services import parse as parse_service
        from apps.resumes.services import upload as upload_service
        from apps.resumes.tests.factories import SAMPLE_CV, docx_bytes

        upload, _ = upload_service.upload_for_profile(
            profile=self.profile,
            actor=self.profile.user,
            uploaded_file=SimpleUploadedFile("cv.docx", docx_bytes(SAMPLE_CV)),
        )
        Attachment.objects.filter(pk=upload.attachment_id).update(scan_status="clean")
        upload.refresh_from_db()
        parse_service.parse_upload(upload=upload)

        self.assertContains(
            self.client.get(reverse("resume:ai")), "Start from what I have already given"
        )

        self.start(token="no-reuse")
        blank = ResumeDocument.objects.get(profile=self.profile)
        self.assertNotIn("Nexa Payments", str(blank.draft.content))
        document_service.delete(document=blank, actor=self.profile.user)

        self.start(token="reuse", reuse="1")
        reused = ResumeDocument.objects.get(profile=self.profile)
        self.assertIn("Nexa Payments", str(reused.draft.content))


class NoProfileYetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)

    def test_new_works_for_someone_who_has_no_candidate_profile_yet(self):
        from apps.accounts.tests.factories import create_user

        user = create_user()
        self.client.force_login(user)
        page = self.client.get(reverse("resume:ai") + "?new=1")
        self.assertContains(page, "Describe the CV you want")


class ConversationPageTests(AssistedFixture):
    def test_the_composer_comes_first_then_review_then_the_checklist(self):
        self.start()
        document = ResumeDocument.objects.get(profile=self.profile)
        html = self.client.get(
            reverse("resume:assistant", kwargs={"document_id": document.public_id})
        ).content.decode()
        talk = html.index("Talk it through")
        review = html.index("Review and edit your CV")
        checklist = html.index("Improve against your latest analysis")
        self.assertLess(talk, review)
        self.assertLess(review, checklist)
        self.assertIn('name="text"', html[talk:review])


class ReplyQualityTests(TestCase):
    def test_a_question_is_not_asked_twice_in_other_words(self):
        reply = (
            "These are good points. Can you tell me more about the specific tasks you "
            "handled during your shifts on weekends?"
        )
        self.assertTrue(
            assistant_service._mentions(
                reply, "What specific tasks did you handle during your shifts on weekends?"
            )
        )
        self.assertFalse(
            assistant_service._mentions(reply, "Which point-of-sale system did you use?")
        )


class FigureQuestionTests(TestCase):
    def test_a_forgotten_number_is_not_asked_for_again_in_other_words(self):
        reply = (
            "You looked after patients on the ward and trained new nurses. Can you tell me "
            "about the number of patients you typically managed? Where are you based?"
        )
        cleaned = assistant_service._without_figure_questions(reply)
        self.assertNotIn("number of patients", cleaned)
        self.assertIn("Where are you based?", cleaned)
        self.assertTrue(
            assistant_service._forgot_the_figure("I don't remember the exact number of patients.")
        )


class ReuseIsOptInTests(AssistedFixture):
    def test_the_reuse_box_is_never_pre_ticked(self):
        from apps.resumes.models import ResumeUpload

        with mock.patch.object(ResumeUpload.objects, "filter") as uploads:
            uploads.return_value.order_by.return_value.first.return_value = mock.Mock(
                original_filename="cv.docx"
            )
            html = self.client.get(reverse("resume:ai")).content.decode()
        box = html[html.index('name="reuse"') - 40 : html.index('name="reuse"') + 120]
        self.assertNotIn("checked", box)


class AssistedQuotaTests(AssistedFixture):
    def setUp(self):
        super().setUp()
        self.document = document_service.create_document(
            profile=self.profile, actor=self.profile.user, mode=BuilderMode.ASSISTANT
        )
        go_on = evidence.Step(question="", latest="", state={})
        patcher = mock.patch.object(assistant_service.evidence, "next_step", return_value=go_on)
        patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self):
        return assistant_service.respond(
            document=self.document, actor=self.profile.user, text=DESCRIPTION
        )

    def test_a_model_failure_does_not_use_a_reply(self):
        failed = AIRun.objects.create(task_key="resume.section_draft", status=RunStatus.TIMEOUT)
        with mock.patch.object(assistant_service.runner, "run", return_value=failed):
            self.respond()
        self.assertEqual(entitlements.remaining(self.profile, "assistant_replies"), 20)

    def test_at_the_limit_the_conversation_says_so_and_keeps_what_was_written(self):
        for _ in range(20):
            entitlements.claim(self.profile, "assistant_replies")
        with mock.patch.object(assistant_service.runner, "run") as model:
            reply = self.respond()
        model.assert_not_called()
        self.assertIn("You have used today's assisted CV replies", reply.text)
        self.assertIn("resets on", reply.text)
        transcript = self.document.builder_sessions.get(mode=BuilderMode.ASSISTANT).transcript
        self.assertTrue(any(DESCRIPTION in turn["text"] for turn in transcript))
