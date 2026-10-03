"""
Employer applicant screens, proved through real HTTP.

The service tests cover the rules; these assert the screens are wired to them.
Two properties get most of the attention, because both are things a page can
break without any service noticing:

  * **a foreign application is unreachable** — not merely refused, absent;
  * **contact details are not in the page** until the stage unlocks them
    (ADR-A34). Asserted against the rendered HTML, because that is where the
    leak would be.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.applications.services import pipeline as pipeline_service
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs.tests.factories import create_job, publish


class BoardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company, title="Backend Engineer"))
        self.application = submit(job=self.job, profile=create_profile())
        self.client.force_login(self.owner)

    def test_the_board_lists_this_company_only(self):
        other, _ = company_with_owner(create_user(email="them@example.test"), slug="them-co")
        their_job = publish(create_job(other, title="Their Role"))
        submit(job=their_job, profile=create_profile())

        response = self.client.get(reverse("employer:applicants", args=["acme-co"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backend Engineer")
        self.assertNotContains(response, "Their Role")

    def test_a_bulk_move_moves_the_chosen_applications(self):
        self.client.post(
            reverse("employer:applicants", args=["acme-co"]),
            {"application": [str(self.application.public_id)], "stage": "reviewing"},
        )

        self.application.refresh_from_db()
        self.assertEqual(self.application.stage.key, "reviewing")

    def test_a_bulk_move_cannot_reach_another_company(self):
        other, _ = company_with_owner(create_user(email="them@example.test"), slug="them-co")
        their_application = submit(job=publish(create_job(other)), profile=create_profile())

        self.client.post(
            reverse("employer:applicants", args=["acme-co"]),
            {"application": [str(their_application.public_id)], "stage": "reviewing"},
        )

        their_application.refresh_from_db()
        self.assertEqual(their_application.stage.key, "new")

    def test_a_bulk_close_without_a_reason_changes_nothing(self):
        self.client.post(
            reverse("employer:applicants", args=["acme-co"]),
            {"application": [str(self.application.public_id)], "stage": "declined"},
        )

        self.application.refresh_from_db()
        self.assertTrue(self.application.is_live)


class DetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company))
        self.profile = create_profile(user=create_user(email="candidate@example.test"))
        self.application = submit(job=self.job, profile=self.profile)
        self.client.force_login(self.owner)

    def url(self, name="employer:application_detail"):
        return reverse(name, args=["acme-co", self.application.public_id])

    def test_the_page_opens(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)

    def test_moving_to_interview_with_no_open_times_says_so(self):
        """The recruiter had no idea the calendar was empty.

        They move somebody to interview, the candidate's screen says their
        interview is being arranged, and nothing happens — because nobody has
        published a time for them to pick.
        """
        response = self.client.post(
            reverse("employer:application_act", args=["acme-co", self.application.public_id]),
            {"action": "move", "stage": "interview"},
            follow=True,
        )

        self.assertContains(response, "cannot book one yet")

    def test_moving_to_interview_with_times_open_just_confirms_it(self):
        from apps.interviews.services import scheduling

        start = timezone.now() + dt.timedelta(days=2)
        scheduling.publish_slots(
            membership=self.membership,
            actor=self.owner,
            windows=[(start, start + dt.timedelta(minutes=45))],
        )

        response = self.client.post(
            reverse("employer:application_act", args=["acme-co", self.application.public_id]),
            {"action": "move", "stage": "interview"},
            follow=True,
        )

        self.assertNotContains(response, "cannot book one yet")

    def test_contact_details_are_absent_before_the_interview_stage(self):
        response = self.client.get(self.url())

        self.assertFalse(response.context["can_see_contact"])
        # The address is not in the page at all, not merely hidden.
        self.assertNotContains(response, "candidate@example.test")

    def test_contact_details_appear_once_the_stage_unlocks_them(self):
        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "interview"),
        )

        response = self.client.get(self.url())

        self.assertTrue(response.context["can_see_contact"])
        self.assertContains(response, "candidate@example.test")

    def test_moving_from_the_screen_works(self):
        self.client.post(
            self.url("employer:application_act"), {"action": "move", "stage": "reviewing"}
        )

        self.application.refresh_from_db()
        self.assertEqual(self.application.stage.key, "reviewing")

    def test_an_unknown_action_is_a_404(self):
        response = self.client.post(self.url("employer:application_act"), {"action": "explode"})
        self.assertEqual(response.status_code, 404)

    def test_a_note_is_saved_and_stays_internal(self):
        self.client.post(
            self.url("employer:application_act"),
            {"action": "note", "body": "Strong on SQL"},
        )

        self.assertEqual(self.application.notes.count(), 1)
        # And it is nowhere in the candidate's filtered timeline.
        self.assertFalse(
            self.application.timeline.filter(
                visible_to_candidate=True, type__icontains="note"
            ).exists()
        )

    def test_a_rating_is_saved(self):
        self.client.post(self.url("employer:application_act"), {"action": "rate", "score": "4"})
        self.assertEqual(self.application.ratings.get().score, 4)

    def test_unlocking_contact_early_needs_a_reason(self):
        self.client.post(
            self.url("employer:application_act"), {"action": "reveal_contact", "reason": ""}
        )

        self.application.refresh_from_db()
        self.assertIsNone(self.application.contact_revealed_at)


class CrossTenantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.insider = create_user()
        self.company, _ = company_with_owner(self.insider, slug="acme-co")

        self.outsider = create_user(email="outsider@example.test")
        self.other, _ = company_with_owner(self.outsider, slug="other-co")
        self.their_application = submit(
            job=publish(create_job(self.other)), profile=create_profile()
        )

        self.client.force_login(self.insider)

    def test_a_foreign_application_under_my_slug_is_a_404(self):
        response = self.client.get(
            reverse(
                "employer:application_detail",
                args=["acme-co", self.their_application.public_id],
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_my_slug_with_their_id_cannot_move_their_application(self):
        self.client.post(
            reverse("employer:application_act", args=["acme-co", self.their_application.public_id]),
            {"action": "move", "stage": "reviewing"},
        )

        self.their_application.refresh_from_db()
        self.assertEqual(self.their_application.stage.key, "new")


class StrictScopingTests(TestCase):
    """`strict_assignment_scoping` narrows a recruiter to their own jobs."""

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner, slug="acme-co")
        self.company.strict_assignment_scoping = True
        self.company.save(update_fields=["strict_assignment_scoping"])

        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())

        self.interviewer = create_user(email="interviewer@example.test")
        self.membership = add_member(self.company, self.interviewer, Role.INTERVIEWER)
        self.client.force_login(self.interviewer)

    def test_an_unassigned_interviewer_sees_nothing(self):
        response = self.client.get(
            reverse("employer:application_detail", args=["acme-co", self.application.public_id])
        )
        self.assertEqual(response.status_code, 404)

    def test_an_interviewer_on_its_panel_sees_the_application(self):
        # Job assignment is not how an Interviewer reads (SRS §2.3): being on
        # an interview for the application is.
        from apps.interviews.models import InterviewParticipant, InterviewSession
        from apps.interviews.tests.factories import live_template

        session = InterviewSession.objects.create(
            application=self.application,
            candidate=self.application.candidate,
            template=live_template(),
            kind="live",
            state="scheduled",
        )
        InterviewParticipant.objects.create(session=session, user=self.interviewer)

        response = self.client.get(
            reverse("employer:application_detail", args=["acme-co", self.application.public_id])
        )
        self.assertEqual(response.status_code, 200)

    def test_the_default_is_not_strict(self):
        # A five-person company must not have to assign every job to every
        # recruiter to make the product usable (SRS §2.3).
        loose, _ = company_with_owner(create_user(email="loose@example.test"), slug="loose-co")
        self.assertFalse(loose.strict_assignment_scoping)


class PipelineScreenTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner, slug="acme-co")
        self.client.force_login(self.owner)

    def test_the_screen_shows_the_platform_default_first(self):
        response = self.client.get(reverse("employer:pipeline", args=["acme-co"]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_default"])

    def test_saving_creates_the_company_its_own_stages(self):
        self.client.post(
            reverse("employer:pipeline", args=["acme-co"]),
            {
                "key": ["inbox", "done"],
                "label": ["Inbox", "Done"],
                "kind": ["open", "terminal"],
                "candidate_label": ["", ""],
                "is_visible_to_candidate": ["inbox", "done"],
            },
        )

        self.assertEqual(
            list(self.company.pipeline_stages.values_list("key", flat=True)), ["inbox", "done"]
        )

    def test_a_pipeline_with_nowhere_to_end_is_refused(self):
        self.client.post(
            reverse("employer:pipeline", args=["acme-co"]),
            {"key": ["inbox"], "label": ["Inbox"], "kind": ["open"], "candidate_label": [""]},
        )
        self.assertEqual(self.company.pipeline_stages.count(), 0)


class PoolScreenTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner, slug="acme-co")
        self.client.force_login(self.owner)

    def test_a_pool_can_be_created_and_deleted(self):
        self.client.post(
            reverse("employer:pools", args=["acme-co"]),
            {"action": "create", "name": "Backend 2026"},
        )
        pool = self.company.talent_pools.get()

        self.client.post(
            reverse("employer:pools", args=["acme-co"]),
            {"action": "delete", "pool_id": pool.pk},
        )
        self.assertEqual(self.company.talent_pools.count(), 0)

    def test_another_companys_pool_cannot_be_deleted(self):
        other, _ = company_with_owner(create_user(email="them@example.test"), slug="them-co")
        from apps.applications.services import pools as pool_service

        theirs = pool_service.create_pool(
            company=other, actor=other.memberships.first().user, name="Theirs"
        )

        self.client.post(
            reverse("employer:pools", args=["acme-co"]),
            {"action": "delete", "pool_id": theirs.pk},
        )
        self.assertTrue(other.talent_pools.filter(pk=theirs.pk).exists())
