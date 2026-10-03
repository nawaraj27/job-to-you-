"""
Candidate application screens, proved through real HTTP.

What is asserted here that the service tests cannot: that the screens show the
candidate their own record and nobody else's, that the timeline they read is
the filtered one, and that a stage a company marked private does not reach the
page under its internal name (FR-6.10).
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.applications.models import Application, StageKind
from apps.applications.services import pipeline as pipeline_service
from apps.applications.services import review as review_service
from apps.applications.tests.factories import create_version, seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.services import postings
from apps.jobs.tests.factories import create_job, publish


class ApplyScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.company, _ = company_with_owner(create_user(email="owner@example.test"))
        self.job = publish(create_job(self.company, title="Backend Engineer"))
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.version = create_version(self.profile)
        self.client.force_login(self.user)

    def url(self):
        return reverse("jobs:apply", args=[self.job.slug])

    def test_the_form_opens(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)

    def test_the_form_says_contact_details_are_not_sent(self):
        # ADR-A34, stated where the decision is made.
        response = self.client.get(self.url())
        self.assertContains(response, "Not your phone number or email")

    def test_applying_creates_the_application_and_redirects_to_it(self):
        response = self.client.post(self.url(), {"document": str(self.version.document.public_id)})

        application = Application.objects.get()
        self.assertRedirects(response, reverse("applications:detail", args=[application.public_id]))
        self.assertEqual(application.candidate, self.profile)

    def test_the_main_cv_is_the_one_offered_first(self):
        """`make_primary` calls itself "the CV applications default to".

        This screen did not: it listed whatever had been edited most recently
        and ticked that. A candidate who had been experimenting saw a list of
        drafts with the newest scratch copy selected, no sign of which one they
        had marked as their main CV, and one click between them and sending the
        wrong document.
        """
        from apps.resumes.services import documents as document_service

        main = document_service.create_document(
            profile=self.profile, actor=self.user, name="My real CV"
        )
        document_service.make_primary(document=main, actor=self.user)
        newer = document_service.create_document(
            profile=self.profile, actor=self.user, name="Scratch copy"
        )

        response = self.client.get(self.url())
        offered = list(response.context["documents"])

        self.assertEqual(offered[0], main)
        self.assertIn(newer, offered)
        self.assertContains(response, "Your main CV")

    def test_applying_twice_is_refused_and_says_so(self):
        self.client.post(self.url(), {"document": str(self.version.document.public_id)})
        response = self.client.post(self.url(), {"document": str(self.version.document.public_id)})

        self.assertEqual(Application.objects.count(), 1)
        self.assertEqual(response.status_code, 400)

    def test_signing_out_sends_you_to_sign_in(self):
        self.client.logout()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)

    def test_an_unpublished_posting_cannot_be_applied_to(self):
        draft = create_job(self.company)
        response = self.client.get(reverse("jobs:apply", args=[draft.slug]))
        self.assertEqual(response.status_code, 404)

    def test_a_required_question_must_be_answered(self):
        owner = self.company.memberships.first().user
        postings.add_question(job=self.job, actor=owner, text="Notice period?", is_required=True)

        response = self.client.post(self.url(), {"document": str(self.version.document.public_id)})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Application.objects.count(), 0)


class ApplicationScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user(email="owner@example.test")
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company, title="Backend Engineer"))
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.application = submit(job=self.job, profile=self.profile)
        self.client.force_login(self.user)

    def test_the_list_shows_my_applications(self):
        response = self.client.get(reverse("applications:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backend Engineer")

    def test_the_list_shows_nobody_elses(self):
        other_job = publish(create_job(self.company, title="Somebody Elses Role"))
        submit(job=other_job, profile=create_profile())

        response = self.client.get(reverse("applications:index"))
        self.assertNotContains(response, "Somebody Elses Role")

    def test_another_persons_application_is_a_404(self):
        theirs = submit(job=self.job, profile=create_profile())
        response = self.client.get(reverse("applications:detail", args=[theirs.public_id]))
        self.assertEqual(response.status_code, 404)

    def test_the_detail_page_shows_the_posting_as_it_read_that_day(self):
        postings.update_job(job=self.job, actor=self.owner, title="Renamed Since")

        response = self.client.get(
            reverse("applications:detail", args=[self.application.public_id])
        )

        self.assertContains(response, "Backend Engineer")
        self.assertEqual(response.context["snapshot"]["title"], "Backend Engineer")

    def test_the_frozen_salary_is_written_the_way_every_other_screen_writes_it(self):
        """The one screen a candidate sees *after* applying looked like a dump.

        The snapshot's raw fields were printed straight out: "NPR
        180000.00-280000.00 per month" against "NPR 180,000-280,000 a month"
        on the posting they had just read.
        """
        response = self.client.get(
            reverse("applications:detail", args=[self.application.public_id])
        )

        shown = response.context["snapshot_salary"]
        self.assertNotIn(".00", shown)
        if shown != "Not disclosed":
            self.assertIn(",", shown)

    def test_reaching_the_interview_stage_with_no_times_says_so(self):
        """The screen showed nothing at all.

        Somebody told they were at interview stage, with no time to pick, no
        explanation and no next step, left to decide for themselves whether to
        chase it.
        """
        import datetime as dt

        from django.utils import timezone

        from apps.core import features
        from apps.core.models import FeatureFlag

        FeatureFlag.objects.update_or_create(
            key="interview",
            defaults={
                "is_enabled": True,
                "expires_on": timezone.localdate() + dt.timedelta(days=7),
            },
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)
        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "interview"),
        )

        response = self.client.get(
            reverse("applications:detail", args=[self.application.public_id])
        )

        self.assertTrue(response.context["awaiting_times"])
        self.assertContains(response, "being arranged")
        self.assertContains(response, "nothing for you to do")

    def test_the_timeline_is_the_filtered_one(self):
        review_service.add_note(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            body="Internal only",
        )

        response = self.client.get(
            reverse("applications:detail", args=[self.application.public_id])
        )

        self.assertNotContains(response, "Internal only")
        for entry in response.context["timeline"]:
            self.assertTrue(entry.visible_to_candidate)

    def test_a_private_stage_name_never_reaches_the_page(self):
        # FR-6.10.
        pipeline_service.configure_stages(
            company=self.company,
            actor=self.owner,
            specs=[
                {"key": "new", "label": "New", "kind": StageKind.OPEN},
                {
                    "key": "maybe",
                    "label": "Probably not",
                    "kind": StageKind.OPEN,
                    "is_visible_to_candidate": False,
                    "candidate_label": "Under review",
                },
                {"key": "done", "label": "Done", "kind": StageKind.TERMINAL},
            ],
        )
        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "maybe"),
        )

        response = self.client.get(
            reverse("applications:detail", args=[self.application.public_id])
        )

        self.assertNotContains(response, "Probably not")
        self.assertContains(response, "Under review")

    def test_withdrawing_from_the_screen_works(self):
        self.client.post(
            reverse("applications:detail", args=[self.application.public_id]),
            {"action": "withdraw", "reason": "Took another role"},
        )

        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.withdrawn_at)

    def test_i_cannot_withdraw_somebody_elses(self):
        theirs = submit(job=self.job, profile=create_profile())
        self.client.post(
            reverse("applications:detail", args=[theirs.public_id]), {"action": "withdraw"}
        )

        theirs.refresh_from_db()
        self.assertIsNone(theirs.withdrawn_at)


class OfferScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user(email="owner@example.test")
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.application = submit(job=self.job, profile=self.profile)

        from apps.applications.services import offers as offer_service

        offer = offer_service.draft_offer(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            salary="1200000",
            currency="NPR",
        )
        self.offer = offer_service.send(offer=offer, actor=self.owner, membership=self.membership)
        self.client.force_login(self.user)

    def test_the_offer_appears_on_the_page(self):
        response = self.client.get(
            reverse("applications:detail", args=[self.application.public_id])
        )
        self.assertContains(response, "You have an offer")

    def test_accepting_from_the_screen_works(self):
        self.client.post(
            reverse("applications:detail", args=[self.application.public_id]),
            {"action": "accept_offer"},
        )

        self.offer.refresh_from_db()
        self.assertEqual(self.offer.state, "accepted")

    def test_declining_records_the_reason(self):
        self.client.post(
            reverse("applications:detail", args=[self.application.public_id]),
            {"action": "decline_offer", "reason": "Taking another role"},
        )

        self.offer.refresh_from_db()
        self.assertEqual(self.offer.state, "declined")
        self.assertEqual(self.offer.decline_reason, "Taking another role")


class VisibilityScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        self.client.force_login(self.user)

    def test_an_untouched_candidate_is_visible_to_nobody(self):
        response = self.client.get(reverse("applications:visibility"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nobody can see you")

    def test_applying_puts_the_company_on_the_list_with_its_reason(self):
        company, _ = company_with_owner(create_user(email="owner@example.test"))
        submit(job=publish(create_job(company)), profile=self.profile)

        response = self.client.get(reverse("applications:visibility"))

        self.assertContains(response, company.name)
        self.assertContains(response, "They applied to us")
