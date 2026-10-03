"""
Employer job screens, proved through real HTTP.

The service tests already cover the rules. What is asserted here is that the
screens are wired to them — that a posting resolved from a URL is scoped to the
company in that URL, and that a POST from someone without access changes
nothing at all rather than merely rendering an error.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs.models import Job, JobSkill, JobStatus
from apps.jobs.services import postings
from apps.jobs.tests.factories import create_job, create_skill, publish


class JobListScreenTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user, slug="acme-co")
        self.client.force_login(self.user)

    def test_the_list_shows_this_company_only(self):
        create_job(self.company, title="Mine")
        theirs, _ = company_with_owner(create_user(email="them@example.test"), slug="them-co")
        create_job(theirs, title="Theirs")

        response = self.client.get(reverse("employer:job_list", args=["acme-co"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Theirs")

    def test_a_status_filter_narrows_the_list(self):
        publish(create_job(self.company, title="Live Role"))
        create_job(self.company, title="Draft Role")

        response = self.client.get(
            reverse("employer:job_list", args=["acme-co"]), {"status": JobStatus.PUBLISHED}
        )

        self.assertContains(response, "Live Role")
        self.assertNotContains(response, "Draft Role")


class JobCreateScreenTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user, slug="acme-co")
        self.client.force_login(self.user)

    def test_posting_the_form_creates_a_draft(self):
        response = self.client.post(
            reverse("employer:job_create", args=["acme-co"]),
            {"title": "Platform Engineer", "description": "Own the deploy pipeline."},
        )

        job = Job.objects.get(title="Platform Engineer")
        self.assertEqual(job.status, JobStatus.DRAFT)
        self.assertRedirects(
            response, reverse("employer:job_detail", args=["acme-co", job.public_id])
        )

    def test_a_rejected_submission_comes_back_with_what_was_typed(self):
        response = self.client.post(
            reverse("employer:job_create", args=["acme-co"]),
            {"title": "", "description": "Own the deploy pipeline."},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Own the deploy pipeline.", status_code=400)

    def test_a_hand_crafted_field_cannot_reach_the_status(self):
        self.client.post(
            reverse("employer:job_create", args=["acme-co"]),
            {"title": "Sneaky", "status": JobStatus.PUBLISHED, "views_count": "999"},
        )

        job = Job.objects.get(title="Sneaky")
        self.assertEqual(job.status, JobStatus.DRAFT)
        self.assertEqual(job.views_count, 0)


class JobCreateRoleScreenTests(TestCase):
    """A Viewer could post the create form and get a draft (SRS §2.3: Viewer "Read")."""

    def setUp(self):
        self.company, _ = company_with_owner(create_user(), slug="acme-co")
        self.viewer = create_user(email="viewer@example.test")
        add_member(self.company, self.viewer, Role.VIEWER)
        self.client.force_login(self.viewer)

    def test_a_viewer_posting_the_form_creates_nothing(self):
        response = self.client.post(
            reverse("employer:job_create", args=["acme-co"]), {"title": "Sneaky Draft"}
        )

        self.assertRedirects(response, reverse("employer:job_list", args=["acme-co"]))
        self.assertFalse(Job.objects.filter(title="Sneaky Draft").exists())

    def test_a_viewer_is_not_shown_the_form_or_the_button(self):
        form = self.client.get(reverse("employer:job_create", args=["acme-co"]))
        self.assertRedirects(form, reverse("employer:job_list", args=["acme-co"]))

        for name in ("employer:overview", "employer:job_list"):
            with self.subTest(screen=name):
                page = self.client.get(reverse(name, args=["acme-co"]))
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, reverse("employer:job_create", args=["acme-co"]))

    def test_a_recruiter_still_sees_the_button_and_can_create(self):
        recruiter = create_user(email="recruiter@example.test")
        add_member(self.company, recruiter, Role.RECRUITER)
        self.client.force_login(recruiter)

        page = self.client.get(reverse("employer:overview", args=["acme-co"]))
        self.assertContains(page, reverse("employer:job_create", args=["acme-co"]))
        self.client.post(
            reverse("employer:job_create", args=["acme-co"]), {"title": "Recruiter Draft"}
        )
        self.assertTrue(Job.objects.filter(title="Recruiter Draft").exists())


class JobActionScreenTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user, slug="acme-co")
        self.job = create_job(self.company)
        self.client.force_login(self.user)

    def url(self, name="employer:job_act"):
        return reverse(name, args=["acme-co", self.job.public_id])

    def test_publishing_from_the_screen_makes_the_posting_live(self):
        self.client.post(self.url(), {"action": "publish", "row_version": self.job.row_version})

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PUBLISHED)

    def test_publishing_without_skills_says_what_that_costs(self):
        """A live posting with no skills is invisible to the matcher.

        It is scored on location, level and pay alone, so the candidates it was
        written for never see it — and the employer was told "The posting is
        live." and nothing else.
        """
        response = self.client.post(
            self.url(),
            {"action": "publish", "row_version": self.job.row_version},
            follow=True,
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PUBLISHED)
        self.assertContains(response, "cannot be matched")

    def test_publishing_with_skills_just_says_it_is_live(self):
        JobSkill.objects.create(job=self.job, skill=create_skill("Python"))

        response = self.client.post(
            self.url(),
            {"action": "publish", "row_version": self.job.row_version},
            follow=True,
        )

        self.assertContains(response, "The posting is live.")
        self.assertNotContains(response, "cannot be matched")

    def test_the_posting_itself_says_it_is_unmatchable(self):
        self.client.post(self.url(), {"action": "publish", "row_version": self.job.row_version})

        response = self.client.get(
            reverse("employer:job_detail", args=["acme-co", self.job.public_id])
        )

        self.assertContains(response, "cannot be matched to anybody on skills")

    def test_an_unknown_action_is_a_404_rather_than_a_silent_no_op(self):
        response = self.client.post(self.url(), {"action": "explode"})
        self.assertEqual(response.status_code, 404)

    def test_a_stale_row_version_leaves_the_posting_alone(self):
        self.client.post(self.url(), {"action": "publish", "row_version": 99})

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.DRAFT)

    def test_deleting_returns_to_the_list(self):
        response = self.client.post(self.url(), {"action": "delete"})
        self.assertRedirects(response, reverse("employer:job_list", args=["acme-co"]))

        self.job.refresh_from_db()
        self.assertIsNotNone(self.job.deleted_at)


class CrossTenantScreenTests(TestCase):
    """A posting belonging to another company must not be reachable at all."""

    def setUp(self):
        self.insider = create_user()
        self.company, _ = company_with_owner(self.insider, slug="acme-co")

        self.outsider = create_user(email="outsider@example.test")
        self.other, _ = company_with_owner(self.outsider, slug="other-co")
        self.their_job = create_job(self.other, title="Their Role")

        self.client.force_login(self.insider)

    def test_a_foreign_posting_under_my_company_slug_is_a_404(self):
        response = self.client.get(
            reverse("employer:job_detail", args=["acme-co", self.their_job.public_id])
        )
        self.assertEqual(response.status_code, 404)

    def test_my_slug_with_their_id_cannot_be_published(self):
        self.client.post(
            reverse("employer:job_act", args=["acme-co", self.their_job.public_id]),
            {"action": "publish"},
        )

        self.their_job.refresh_from_db()
        self.assertEqual(self.their_job.status, JobStatus.DRAFT)

    def test_their_slug_with_their_id_is_refused_because_i_am_not_a_member(self):
        response = self.client.get(
            reverse("employer:job_detail", args=["other-co", self.their_job.public_id])
        )
        self.assertIn(response.status_code, {403, 404})


class AssignmentAccessTests(TestCase):
    """The negative case that makes assignment worth having."""

    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner, slug="acme-co")
        self.job = create_job(self.company, title="Backend Engineer")

        self.recruiter = create_user(email="recruiter@example.test")
        self.recruiter_membership = add_member(self.company, self.recruiter, Role.RECRUITER)
        self.client.force_login(self.recruiter)

    def test_an_unassigned_recruiter_cannot_edit_and_nothing_changes(self):
        response = self.client.post(
            reverse("employer:job_edit", args=["acme-co", self.job.public_id]),
            {"title": "Renamed by someone without access"},
        )

        self.job.refresh_from_db()
        self.assertNotEqual(self.job.title, "Renamed by someone without access")
        self.assertEqual(response.status_code, 302)

    def test_an_assigned_recruiter_can_edit(self):
        postings.assign(job=self.job, actor=self.owner, membership=self.recruiter_membership)

        self.client.post(
            reverse("employer:job_edit", args=["acme-co", self.job.public_id]),
            {"title": "Renamed by the assignee", "row_version": self.job.row_version},
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.title, "Renamed by the assignee")

    def test_an_assigned_member_without_the_publish_grant_cannot_publish(self):
        # A recruiter holds `job.publish`; an interviewer does not. Assignment
        # buys write access to this posting, never the right to put the
        # company's name on the market.
        interviewer = create_user(email="interviewer@example.test")
        membership = add_member(self.company, interviewer, Role.INTERVIEWER)
        postings.assign(job=self.job, actor=self.owner, membership=membership)
        self.client.force_login(interviewer)

        self.client.post(
            reverse("employer:job_act", args=["acme-co", self.job.public_id]),
            {"action": "publish", "row_version": self.job.row_version},
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.DRAFT)

    def test_an_assigned_recruiter_may_publish_what_they_were_given(self):
        postings.assign(job=self.job, actor=self.owner, membership=self.recruiter_membership)
        self.job.description = "A real description."
        self.job.save(update_fields=["description"])

        self.client.post(
            reverse("employer:job_act", args=["acme-co", self.job.public_id]),
            {"action": "publish", "row_version": self.job.row_version},
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PUBLISHED)

    def test_an_unassigned_recruiter_can_still_read_the_posting(self):
        response = self.client.get(
            reverse("employer:job_detail", args=["acme-co", self.job.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_write"])


class SkillScreenTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user, slug="acme-co")
        self.job = create_job(self.company)
        create_skill("Python")
        self.client.force_login(self.user)

    def test_a_resolved_skill_is_attached(self):
        self.client.post(
            reverse("employer:job_skills", args=["acme-co", self.job.public_id]),
            {"text": "python"},
        )
        self.assertEqual(self.job.skills.count(), 1)

    def test_a_near_miss_still_resolves_rather_than_refusing_a_typo(self):
        # The resolver is spelling-tolerant on purpose; a posting is not the
        # place to punish a missing letter.
        self.client.post(
            reverse("employer:job_skills", args=["acme-co", self.job.public_id]),
            {"text": "Pythonn"},
        )
        self.assertEqual(self.job.skills.count(), 1)

    def test_an_unknown_skill_is_refused_and_nothing_is_attached(self):
        self.client.post(
            reverse("employer:job_skills", args=["acme-co", self.job.public_id]),
            {"text": "Quantum Basket Weaving"},
        )
        self.assertEqual(self.job.skills.count(), 0)


class CompanyProfileScreenTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user, slug="acme-co")
        self.client.force_login(self.user)

    def test_the_profile_saves_paragraphs_in_order(self):
        self.client.post(
            reverse("employer:profile", args=["acme-co"]),
            {
                "row_version": self.company.row_version,
                "name": "Acme",
                "about": "First paragraph.\n\nSecond paragraph.",
            },
        )

        self.company.refresh_from_db()
        self.assertEqual(self.company.about, ["First paragraph.", "Second paragraph."])

    def test_a_stale_row_version_leaves_the_profile_alone(self):
        self.client.post(
            reverse("employer:profile", args=["acme-co"]),
            {"row_version": 99, "name": "Renamed", "about": ""},
        )

        self.company.refresh_from_db()
        self.assertNotEqual(self.company.name, "Renamed")

    def test_a_member_without_company_edit_cannot_change_it(self):
        interviewer = create_user(email="interviewer@example.test")
        add_member(self.company, interviewer, Role.INTERVIEWER)
        self.client.force_login(interviewer)

        self.client.post(
            reverse("employer:profile", args=["acme-co"]),
            {"row_version": self.company.row_version, "name": "Renamed", "about": ""},
        )

        self.company.refresh_from_db()
        self.assertNotEqual(self.company.name, "Renamed")
