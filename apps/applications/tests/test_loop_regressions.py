"""
Defects reproduced on the running product in the Jobs/Applications loop.

Each test is the failing side of something seen on the development database:

  * a refused application still saved the letter typed on the apply screen,
    and the error page came back without it (one account had ten identical
    "For Backend Engineer II" letters for a role it never applied to);
  * a saved letter chosen alongside a typed one silently discarded the typing;
  * a private stage's internal name reached the candidate by notification and
    email, though every screen showed the public label (FR-6.10);
  * closing four applicants in bulk with no reason reported "Moved 0 to Not
    proceeding. 4 were already there.";
  * the employer's application page printed "30.833333333333336 / 50" and
    raw event codes such as "application.submitted".
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.applications.models import (
    Application,
    CoverLetter,
    PipelineStage,
    StageKind,
)
from apps.applications.services import apply as apply_service
from apps.applications.services import pipeline as pipeline_service
from apps.applications.tests.factories import create_version, seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import OutboxEvent

LETTER = "I rebuilt a settlement ledger and would like to do it again here."


class ApplyLetterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user(email="owner@example.test")
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company, title="Backend Engineer II"))
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.version = create_version(self.profile)
        self.client.force_login(self.user)

    def post(self, **fields):
        data = {"document": str(self.version.document.public_id), **fields}
        return self.client.post(reverse("jobs:apply", args=[self.job.slug]), data)

    def test_a_refused_application_saves_no_letter(self):
        self.post()  # applied once
        letters_before = CoverLetter.objects.count()

        response = self.post(cover_letter_text=LETTER)  # refused: already applied

        self.assertEqual(response.status_code, 400)
        self.assertEqual(CoverLetter.objects.count(), letters_before)

    def test_the_typed_letter_comes_back_with_the_error(self):
        # A refusal that keeps the form on screen: a required question left
        # blank. ("Already applied" replaces the form with a link to the
        # existing application, which is right, and has no letter to return.)
        from apps.jobs.services import postings

        postings.add_question(
            job=self.job, actor=self.owner, text="Notice period?", is_required=True
        )

        response = self.post(cover_letter_text=LETTER)

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, LETTER, status_code=400)
        self.assertEqual(CoverLetter.objects.count(), 0)

    def test_a_letter_typed_on_a_successful_application_is_saved_and_attached(self):
        self.post(cover_letter_text=LETTER)

        application = Application.objects.get()
        self.assertIsNotNone(application.cover_letter)
        self.assertEqual(application.cover_letter.content, LETTER)
        self.assertEqual(CoverLetter.objects.count(), 1)

    def test_choosing_a_saved_letter_and_typing_one_is_refused_not_silently_resolved(self):
        saved = CoverLetter.objects.create(profile=self.profile, title="Old", content="Old letter")

        response = self.post(cover_letter=str(saved.public_id), cover_letter_text=LETTER)

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "not both", status_code=400)
        self.assertEqual(Application.objects.count(), 0)
        self.assertContains(response, LETTER, status_code=400)

    def test_saved_letters_can_be_told_apart(self):
        CoverLetter.objects.create(
            profile=self.profile,
            title="For Backend Engineer II",
            content="First version about ledgers",
        )
        CoverLetter.objects.create(
            profile=self.profile,
            title="For Backend Engineer II",
            content="Second version about payouts",
        )

        response = self.client.get(reverse("jobs:apply", args=[self.job.slug]))

        self.assertContains(response, "First version about ledgers")
        self.assertContains(response, "Second version about payouts")

    def test_the_chosen_cv_stays_chosen_after_an_error(self):
        from apps.resumes.services import documents as document_service

        other = document_service.create_document(
            profile=self.profile, actor=self.user, name="Second CV"
        )
        self.post()  # applied, so the next attempt is refused
        response = self.post(document=str(other.public_id))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["posted"]["document"], str(other.public_id))


class PrivateStageNotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())
        self.private = PipelineStage.objects.create(
            company=self.company,
            key="hm-doubts",
            label="Hiring manager doubts",
            order=25,
            kind=StageKind.OPEN,
            is_visible_to_candidate=False,
            candidate_label="Under review",
        )

    def test_the_notification_payload_carries_the_public_label(self):
        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=self.private,
        )

        event = OutboxEvent.objects.filter(type="application.stage_changed").latest("pk")
        self.assertEqual(event.payload["stage_label"], "Under review")
        # The payload keeps the stage *key* for subscribers; the *label* a
        # company marked private is what must never travel to the candidate.
        self.assertNotIn("Hiring manager doubts", str(event.payload))

    def test_the_rendered_notification_does_not_name_the_private_stage(self):
        from apps.notifications.events import spec_for
        from apps.notifications.services.channels import render

        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=self.private,
        )
        event = OutboxEvent.objects.filter(type="application.stage_changed").latest("pk")
        title, body = render(spec_for(event.type), event.payload)

        self.assertIn("Under review", body)
        self.assertNotIn("doubts", title + body)
        self.assertNotIn("moved you on", title)

    def test_the_home_activity_uses_the_public_label(self):
        from portals.candidate.views import _activity

        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=self.private,
        )
        texts = [row["text"] for row in _activity(self.application.candidate)]

        self.assertTrue(any("Under review" in text for text in texts), texts)
        self.assertFalse(any("doubts" in text for text in texts))


class BulkMoveHonestyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company))
        self.applications = [submit(job=self.job, profile=create_profile()) for _ in range(3)]

    def test_a_bulk_close_without_a_reason_is_refused_once(self):
        with self.assertRaisesMessage(InvalidState, "Say why"):
            pipeline_service.move_many(
                applications=self.applications,
                actor=self.owner,
                membership=self.membership,
                stage=pipeline_service.stage_by_key(self.company, "declined"),
            )

    def test_the_board_says_why_nothing_moved(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("employer:applicants", args=["acme-co"]),
            {"application": [str(a.public_id) for a in self.applications], "stage": "declined"},
            follow=True,
        )

        self.assertContains(response, "Say why these applications are being closed")
        self.assertNotContains(response, "were already there")

    def test_a_withdrawn_candidate_is_reported_as_not_moved_rather_than_already_there(self):
        withdrawn = self.applications[0]
        apply_service.withdraw(application=withdrawn, actor=withdrawn.candidate.user)
        withdrawn.refresh_from_db()

        result = pipeline_service.move_many(
            applications=self.applications,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "reviewing"),
        )

        self.assertEqual(result["moved"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["refused"], 1)
        self.assertIn("withdrew", result["reasons"][0])


class EmployerDetailReadabilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())
        self.application.match_snapshot = {
            "score": 64,
            "band": "partial",
            "components": [
                {"label": "Skills", "points": 30.833333333333336, "weight": 50, "note": ""},
                {"label": "Freshness", "points": 4.066666666666666, "weight": 5, "note": ""},
            ],
        }
        self.application.save(update_fields=["match_snapshot"])
        self.client.force_login(self.owner)

    def page(self):
        return self.client.get(
            reverse("employer:application_detail", args=["acme-co", self.application.public_id])
        )

    def test_match_points_are_rounded(self):
        response = self.page()
        self.assertContains(response, "30.8 / 50")
        self.assertNotContains(response, "30.833333333333336")

    def test_the_activity_log_uses_words_not_event_codes(self):
        response = self.page()
        self.assertContains(response, "Applied")
        self.assertNotContains(response, ">application.submitted<")
        self.assertNotIn(
            "application.submitted", response.content.decode().split("Activity", 1)[-1][:2000]
        )
