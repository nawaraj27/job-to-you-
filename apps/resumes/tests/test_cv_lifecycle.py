"""
Saved CVs, unsaved drafts, expiry and deletion (docs/CV-Lifecycle.md).

  * Opening the builder or generating a CV makes an unsaved draft, never a
    saved CV. It becomes saved the first time a version is frozen.
  * "Your CVs" (hub and dashboard) lists saved CVs only; drafts are listed
    apart, each with the day it expires.
  * Drafts are capped; abandoned ones are removed by an idempotent task that
    never touches saved or archived CVs.
  * Delete asks first; a never-saved draft is removed completely and its old
    links stop working; another candidate's CV cannot be touched.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.candidates.tests.factories import create_profile
from apps.common.errors import Conflict
from apps.resumes import selectors
from apps.resumes.models import ResumeDocument, ResumeDraft, ResumeTemplate, VersionReason
from apps.resumes.services import documents as document_service
from apps.resumes.services import versions as version_service
from apps.resumes.tasks import purge_abandoned_drafts

HX = {"HTTP_HX_REQUEST": "true"}


class LifecycleFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.user = self.profile.user
        self.client.force_login(self.user)

    def new_draft(self, name: str = "Draft CV") -> ResumeDocument:
        from apps.resumes import examples

        return document_service.create_document(
            profile=self.profile,
            actor=self.user,
            name=name,
            content=examples.example_content(name="Test Person", email="t@example.com"),
        )

    def age(self, document: ResumeDocument, days: int) -> None:
        then = timezone.now() - dt.timedelta(days=days)
        ResumeDocument.objects.filter(pk=document.pk).update(updated_at=then)
        ResumeDraft.objects.filter(document=document).update(updated_at=then)

    def save(self, document: ResumeDocument) -> None:
        version_service.finalize(document=document, actor=self.user, reason=VersionReason.SAVED)


class SavedOrDraftTests(LifecycleFixture):
    def test_a_new_cv_is_an_unsaved_draft(self):
        document = self.new_draft()
        self.assertIsNone(document.saved_at)
        self.assertNotIn(document, selectors.saved_documents_for(self.profile))
        drafts = selectors.drafts_for(self.profile)
        self.assertEqual([d.pk for d in drafts], [document.pk])
        self.assertEqual(drafts[0].expires_on, (timezone.now() + dt.timedelta(days=14)).date())

    def test_opening_the_builder_creates_a_draft_not_a_saved_cv(self):
        self.client.post(
            reverse("resume:builder"),
            {"name": "Opened once", "template": "meridian", "mode": "editor"},
        )
        document = ResumeDocument.objects.get(profile=self.profile)
        self.assertIsNone(document.saved_at)
        hub = self.client.get(reverse("resume:builder"))
        self.assertContains(hub, "No saved CVs yet")
        self.assertContains(hub, "Unsaved drafts")
        self.assertContains(hub, "Opened once")
        self.assertContains(hub, "unless you save it")

    def test_saving_a_version_makes_it_a_saved_cv(self):
        document = self.new_draft("Product CV")
        act = reverse("resume:builder_act", kwargs={"document_id": document.public_id})
        self.client.post(act, {"command": "save_version", "note": "first"}, **HX)

        document.refresh_from_db()
        self.assertIsNotNone(document.saved_at)
        self.assertIn(document, selectors.saved_documents_for(self.profile))
        self.assertEqual(selectors.drafts_for(self.profile), [])
        # An established candidate (a brand-new one is shown onboarding instead).
        from apps.candidates.models import CandidateProfile

        CandidateProfile.objects.filter(pk=self.profile.pk).update(completeness=40)
        dashboard = self.client.get(reverse("dashboard:index"))
        self.assertContains(dashboard, "Your CVs")
        self.assertContains(dashboard, "Product CV")
        self.assertNotContains(dashboard, "Unsaved drafts")

    def test_every_act_that_freezes_a_version_saves(self):
        for reason in VersionReason.values:
            with self.subTest(reason=reason):
                document = self.new_draft(f"CV {reason}")
                version_service.finalize(document=document, actor=self.user, reason=reason)
                document.refresh_from_db()
                self.assertIsNotNone(document.saved_at)

    def test_saving_twice_keeps_the_first_save_time(self):
        document = self.new_draft()
        self.save(document)
        first = ResumeDocument.objects.get(pk=document.pk).saved_at
        self.save(document)
        self.assertEqual(ResumeDocument.objects.get(pk=document.pk).saved_at, first)

    def test_the_editor_says_it_is_an_unsaved_draft(self):
        document = self.new_draft()
        page = self.client.get(reverse("resume:editor", kwargs={"document_id": document.public_id}))
        self.assertContains(page, "This is an unsaved draft")


class DraftLimitTests(LifecycleFixture):
    def test_drafts_are_capped_and_saved_cvs_do_not_count(self):
        saved = self.new_draft("Saved")
        self.save(saved)
        for index in range(document_service.MAX_DRAFTS):
            self.new_draft(f"Draft {index}")
        with self.assertRaises(Conflict) as caught:
            self.new_draft("One too many")
        self.assertIn("Save one or delete one", caught.exception.message)


class ExpiryTests(LifecycleFixture):
    def test_an_abandoned_draft_is_removed(self):
        document = self.new_draft()
        self.age(document, 15)
        self.assertEqual(purge_abandoned_drafts(), 1)
        self.assertFalse(ResumeDocument.objects.filter(pk=document.pk).exists())

    def test_a_recent_draft_is_kept(self):
        document = self.new_draft()
        self.age(document, 13)
        self.assertEqual(purge_abandoned_drafts(), 0)
        self.assertTrue(ResumeDocument.objects.filter(pk=document.pk).exists())

    def test_saved_and_archived_cvs_are_never_removed(self):
        saved = self.new_draft("Saved")
        self.save(saved)
        archived = self.new_draft("Archived draft")
        document_service.archive(document=archived, actor=self.user)
        for document in (saved, archived):
            self.age(document, 400)
        self.assertEqual(purge_abandoned_drafts(), 0)
        self.assertTrue(ResumeDocument.objects.filter(pk=saved.pk).exists())
        self.assertTrue(ResumeDocument.objects.filter(pk=archived.pk).exists())

    def test_the_purge_is_idempotent(self):
        self.age(self.new_draft(), 30)
        self.assertEqual(purge_abandoned_drafts(), 1)
        self.assertEqual(purge_abandoned_drafts(), 0)

    @override_settings(RESUME_DRAFT_RETENTION_DAYS=3)
    def test_the_retention_period_is_configurable(self):
        document = self.new_draft()
        self.age(document, 4)
        self.assertEqual(purge_abandoned_drafts(), 1)

    def test_the_purge_is_scheduled(self):
        from config.celery import app

        tasks = {entry["task"] for entry in app.conf.beat_schedule.values()}
        self.assertIn("apps.resumes.tasks.purge_abandoned_drafts", tasks)


class DeleteTests(LifecycleFixture):
    def act(self, command: str, document: ResumeDocument):
        return self.client.post(
            reverse("resume:document_act"),
            {"command": command, "document": str(document.public_id)},
            **HX,
        )

    def test_the_delete_buttons_ask_first(self):
        self.new_draft()
        hub = self.client.get(reverse("resume:builder"))
        self.assertContains(hub, 'hx-confirm="Delete the draft')
        self.assertContains(hub, 'data-confirm-tone="danger"')

    def test_deleting_a_never_saved_draft_removes_it_and_its_links(self):
        document = self.new_draft()
        editor = reverse("resume:editor", kwargs={"document_id": document.public_id})
        response = self.act("delete", document)
        self.assertContains(response, "Deleted")
        self.assertFalse(ResumeDocument.objects.filter(pk=document.pk).exists())
        self.assertFalse(ResumeDraft.objects.filter(document_id=document.pk).exists())
        self.assertEqual(self.client.get(editor).status_code, 302)  # back to the hub
        self.assertEqual(
            self.client.post(
                reverse("resume:builder_act", kwargs={"document_id": document.public_id}),
                {"command": "save_version"},
                **HX,
            ).status_code,
            404,
        )

    def test_deleting_a_saved_cv_hides_it_and_keeps_its_versions(self):
        document = self.new_draft()
        self.save(document)
        self.act("delete", document)
        document.refresh_from_db()
        self.assertIsNotNone(document.deleted_at)
        self.assertTrue(document.versions.exists())
        self.assertNotIn(document, selectors.saved_documents_for(self.profile))

    def test_another_candidates_cv_cannot_be_deleted(self):
        document = self.new_draft()
        self.client.force_login(create_profile().user)
        self.assertEqual(self.act("delete", document).status_code, 404)
        self.assertTrue(ResumeDocument.objects.filter(pk=document.pk).exists())

    def test_the_messages_read_naturally(self):
        document = self.new_draft()
        self.save(document)
        other = self.new_draft("Other")
        self.save(other)
        response = self.act("make_primary", other)
        self.assertContains(response, "Set as your main CV")


class MissingDraftRowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)

    def test_a_document_without_a_draft_row_still_lists(self):
        profile = create_profile()
        document = ResumeDocument.objects.create(
            profile=profile, name="Imported", template=ResumeTemplate.objects.first()
        )
        self.assertFalse(ResumeDraft.objects.filter(document=document).exists())
        rows = selectors.drafts_for(profile)
        self.assertEqual([row.pk for row in rows], [document.pk])
        self.assertIsNotNone(rows[0].expires_on)


class ExpiredMainDraftTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def test_an_expired_main_draft_hands_main_to_a_saved_cv(self):
        profile = create_profile()
        main = document_service.create_document(profile=profile, actor=profile.user, name="First")
        saved = document_service.create_document(profile=profile, actor=profile.user, name="Kept")
        version_service.finalize(document=saved, actor=profile.user, reason=VersionReason.SAVED)
        self.assertTrue(main.is_primary)

        later = timezone.now() + dt.timedelta(days=30)
        ResumeDraft.objects.filter(document=main).update(updated_at=later - dt.timedelta(days=20))
        ResumeDocument.objects.filter(pk=main.pk).update(updated_at=later - dt.timedelta(days=20))
        ResumeDraft.objects.filter(document=saved).update(updated_at=later)
        ResumeDocument.objects.filter(pk=saved.pk).update(updated_at=later)

        self.assertEqual(document_service.purge_abandoned_drafts(now=later), 1)
        saved.refresh_from_db()
        self.assertTrue(saved.is_primary)


class OverviewForANewProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def test_saved_cvs_show_before_the_profile_is_filled_in(self):
        # Assisted CV is the main way in, and it needs no profile fields: the
        # overview must not hide the CV just because the profile is empty.
        profile = create_profile()
        self.client.force_login(profile.user)
        document = document_service.create_document(
            profile=profile, actor=profile.user, name="Nursing CV"
        )
        version_service.finalize(document=document, actor=profile.user, reason=VersionReason.SAVED)
        page = self.client.get(reverse("dashboard:index"))
        self.assertTrue(page.context["is_new"])  # the onboarding state
        self.assertContains(page, "Start an Assisted CV")
        self.assertContains(page, "Your CVs")
        self.assertContains(page, "Nursing CV")
