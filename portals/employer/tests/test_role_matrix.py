"""
The employer role matrix of SRS §2.3, asserted on real URLs and POSTs.

Built by the whole-site loop after a route sweep showed every company role —
Interviewer and Viewer included — reading every applicant, every candidate
conversation, billing and webhooks, and able to move, tag, note and rate any
application and cancel any interview. Hiding the buttons was never the control;
these tests post to the endpoints directly.

Rows, from §2.3:

  * Interviewer — Jobs, Team, Company, Billing: None; applications only for
    interviews they are on, until thirty days after.
  * Viewer — lists and aggregates; no candidate detail; read-only everywhere.
  * Hiring Manager — assigned jobs only; comment and rate; moves only if granted.
  * Recruiter — reads and writes on assigned jobs only (decision D-13);
    creating a job assigns its author.
  * Billing — Owner and Company Admin see it; only the Owner changes it (D-11).
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.applications.models import Application, ApplicationNote, ApplicationRating
from apps.candidates.tests.factories import create_profile
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.interviews.models import InterviewParticipant, InterviewSession, SessionState
from apps.interviews.tests.factories import live_template
from apps.jobs.models import Job, JobAssignment
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for


class MatrixFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, self.owner_membership = company_with_owner(self.owner)
        self.members = {"owner": self.owner_membership}
        for role in (
            Role.ADMIN,
            Role.RECRUITER,
            Role.HIRING_MANAGER,
            Role.INTERVIEWER,
            Role.VIEWER,
        ):
            self.members[role] = add_member(self.company, create_profile().user, role=role)

        self.job = publish(create_job(self.company, title="Backend Engineer"))
        self.other_job = publish(create_job(self.company, title="Designer"))
        self.candidate = create_profile()
        self.application = application_for(self.candidate, self.job)
        self.other_application = application_for(create_profile(), self.other_job)
        self.slug = self.company.slug

    def as_role(self, role):
        self.client.force_login(self.members[role].user)
        return self.members[role]

    def url(self, name, *args):
        return reverse(name, args=[self.slug, *args])

    def detail(self, application=None):
        return self.url("employer:application_detail", (application or self.application).public_id)

    def act(self, application=None):
        return self.url("employer:application_act", (application or self.application).public_id)

    def assign(self, role, job=None):
        JobAssignment.objects.create(job=job or self.job, membership=self.members[role])


class SectionAccessTests(MatrixFixture):
    SECTIONS = {
        "employer:job_list": ("owner", "admin", "recruiter", "hiring_manager", "viewer"),
        "employer:team": ("owner", "admin", "recruiter", "hiring_manager", "viewer"),
        "employer:profile": ("owner", "admin", "recruiter", "hiring_manager", "viewer"),
        "employer:verification": ("owner", "admin", "recruiter", "hiring_manager", "viewer"),
        "employer:pipeline": ("owner", "admin", "recruiter", "hiring_manager", "viewer"),
        "employer:webhooks": ("owner", "admin", "recruiter", "hiring_manager", "viewer"),
        "employer:billing": ("owner", "admin"),
        "employer:messages": ("owner", "admin", "recruiter"),
        "employer:talent": ("owner", "admin", "recruiter"),
        "employer:pools": ("owner", "admin", "recruiter"),
    }

    def test_each_section_opens_only_for_the_roles_that_have_it(self):
        for name, allowed in self.SECTIONS.items():
            for role in self.members:
                with self.subTest(section=name, role=role):
                    self.as_role(role)
                    status = self.client.get(self.url(name)).status_code
                    if role in allowed:
                        self.assertEqual(status, 200)
                    else:
                        self.assertEqual(status, 403)

    def test_the_rail_offers_no_section_the_role_cannot_open(self):
        for role in self.members:
            with self.subTest(role=role):
                self.as_role(role)
                rail = self.client.get(self.url("employer:overview")).context["rail_nav"]
                offered = {item["url_name"] for group in rail for item in _items(group)}
                for name, allowed in self.SECTIONS.items():
                    if name in offered:
                        self.assertIn(role, allowed, f"{role} is offered {name}")


def _items(group):
    """Every link in one rail group, children included."""
    links = list(group.get("links", []))
    for link in list(links):
        links.extend(link.get("children", []))
    return links


class ApplicationReadScopeTests(MatrixFixture):
    def test_a_hiring_manager_sees_only_applicants_to_assigned_jobs(self):
        self.assign(Role.HIRING_MANAGER)
        self.as_role(Role.HIRING_MANAGER)

        self.assertEqual(self.client.get(self.detail()).status_code, 200)
        self.assertEqual(self.client.get(self.detail(self.other_application)).status_code, 404)
        board = self.client.get(self.url("employer:applicants"))
        self.assertContains(board, self.candidate.user.full_name)
        self.assertNotContains(board, self.other_application.candidate.user.full_name)

    def test_an_interviewer_sees_only_applications_they_interview_for(self):
        membership = self.as_role(Role.INTERVIEWER)
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.assertNotContains(
            self.client.get(self.url("employer:applicants")), self.candidate.user.full_name
        )

        session = InterviewSession.objects.create(
            application=self.application,
            candidate=self.candidate,
            template=live_template(),
            kind="live",
            state=SessionState.SCHEDULED,
            scheduled_at=timezone.now() + dt.timedelta(days=1),
        )
        InterviewParticipant.objects.create(session=session, user=membership.user)

        self.assertEqual(self.client.get(self.detail()).status_code, 200)
        self.assertEqual(self.client.get(self.detail(self.other_application)).status_code, 404)

        # Thirty days after the interview ended, the access ends too.
        InterviewSession.objects.filter(pk=session.pk).update(
            state=SessionState.COMPLETED, ended_at=timezone.now() - dt.timedelta(days=31)
        )
        self.assertEqual(self.client.get(self.detail()).status_code, 404)

    def test_a_viewer_reads_the_list_but_not_the_candidate(self):
        self.as_role(Role.VIEWER)

        self.assertContains(
            self.client.get(self.url("employer:applicants")), self.candidate.user.full_name
        )
        self.assertEqual(self.client.get(self.detail()).status_code, 403)

    def test_strict_scoping_narrows_a_recruiter_to_assignments(self):
        self.company.strict_assignment_scoping = True
        self.company.save(update_fields=["strict_assignment_scoping"])
        self.assign(Role.RECRUITER)
        self.as_role(Role.RECRUITER)

        self.assertEqual(self.client.get(self.detail()).status_code, 200)
        self.assertEqual(self.client.get(self.detail(self.other_application)).status_code, 404)

    def test_strict_scoping_does_not_narrow_an_admin(self):
        self.company.strict_assignment_scoping = True
        self.company.save(update_fields=["strict_assignment_scoping"])
        self.as_role(Role.ADMIN)

        self.assertEqual(self.client.get(self.detail(self.other_application)).status_code, 200)

    def test_another_company_never_sees_the_application(self):
        stranger = create_profile().user
        company_with_owner(stranger)
        self.client.force_login(stranger)

        self.assertEqual(self.client.get(self.detail()).status_code, 404)


class ApplicationWriteTests(MatrixFixture):
    def post_note(self, role):
        self.as_role(role)
        return self.client.post(self.act(), {"action": "note", "body": f"note by {role}"})

    def test_read_only_roles_cannot_note_rate_tag_or_move(self):
        from apps.interviews.models import InterviewParticipant as Panel

        session = InterviewSession.objects.create(
            application=self.application,
            candidate=self.candidate,
            template=live_template(),
            kind="live",
            state=SessionState.SCHEDULED,
            scheduled_at=timezone.now() + dt.timedelta(days=1),
        )
        Panel.objects.create(session=session, user=self.members[Role.INTERVIEWER].user)
        stage_before = self.application.stage_id

        for role in (Role.VIEWER, Role.INTERVIEWER):
            with self.subTest(role=role):
                self.as_role(role)
                for data in (
                    {"action": "note", "body": "x"},
                    {"action": "rate", "score": "5"},
                    {"action": "tag", "tag": "x"},
                    {"action": "move", "stage": "reviewing"},
                ):
                    self.client.post(self.act(), data)

        self.assertFalse(ApplicationNote.objects.filter(application=self.application).exists())
        self.assertFalse(ApplicationRating.objects.filter(application=self.application).exists())
        self.assertFalse(self.application.tag_links.exists())
        self.application.refresh_from_db()
        self.assertEqual(self.application.stage_id, stage_before)

    def test_an_unassigned_recruiter_neither_reads_nor_moves(self):
        # Decision D-13: recruiters used to read every job's applicants.
        self.as_role(Role.RECRUITER)
        stage_before = self.application.stage_id

        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.client.post(self.act(), {"action": "move", "stage": "reviewing"})

        self.application.refresh_from_db()
        self.assertEqual(self.application.stage_id, stage_before)

    def test_an_assigned_recruiter_moves_and_notes(self):
        self.assign(Role.RECRUITER)
        self.as_role(Role.RECRUITER)

        self.client.post(self.act(), {"action": "note", "body": "Strong on SQL"})
        self.client.post(self.act(), {"action": "move", "stage": "reviewing"})

        self.application.refresh_from_db()
        self.assertEqual(self.application.stage.key, "reviewing")
        self.assertTrue(ApplicationNote.objects.filter(application=self.application).exists())

    def test_a_hiring_manager_comments_but_moves_only_when_granted(self):
        self.assign(Role.HIRING_MANAGER)
        membership = self.as_role(Role.HIRING_MANAGER)

        self.client.post(self.act(), {"action": "note", "body": "Good call"})
        self.client.post(self.act(), {"action": "rate", "score": "4"})
        self.client.post(self.act(), {"action": "move", "stage": "reviewing"})

        self.application.refresh_from_db()
        self.assertTrue(ApplicationNote.objects.filter(application=self.application).exists())
        self.assertTrue(ApplicationRating.objects.filter(application=self.application).exists())
        self.assertNotEqual(self.application.stage.key, "reviewing")

        membership.permissions = ["application.decide"]
        membership.save(update_fields=["permissions"])
        self.client.post(self.act(), {"action": "move", "stage": "reviewing"})
        self.application.refresh_from_db()
        self.assertEqual(self.application.stage.key, "reviewing")

    def test_bulk_moves_leave_applications_the_member_cannot_change(self):
        self.assign(Role.RECRUITER)
        self.as_role(Role.RECRUITER)

        response = self.client.post(
            self.url("employer:applicants"),
            {
                "application": [self.application.public_id, self.other_application.public_id],
                "stage": "reviewing",
            },
            follow=True,
        )

        self.application.refresh_from_db()
        self.other_application.refresh_from_db()
        self.assertEqual(self.application.stage.key, "reviewing")
        self.assertNotEqual(self.other_application.stage.key, "reviewing")
        # The unassigned row is not even readable (D-13), so it is not counted
        # as one "left alone" — it was never in reach.
        self.assertContains(response, "Moved 1 to")

    def test_a_viewer_is_not_offered_the_forms(self):
        self.as_role(Role.ADMIN)
        admin_page = self.client.get(self.detail()).content.decode()
        self.assertIn('value="move"', admin_page)

        self.assign(Role.HIRING_MANAGER)
        self.as_role(Role.HIRING_MANAGER)
        manager_page = self.client.get(self.detail()).content.decode()
        self.assertNotIn('value="move"', manager_page)
        self.assertIn('value="note"', manager_page)
        self.assertNotIn(
            reverse("employer:application_message", args=[self.slug, self.application.public_id]),
            manager_page,
        )


class JobMatrixTests(MatrixFixture):
    def test_a_recruiter_can_edit_the_job_they_just_created(self):
        self.as_role(Role.RECRUITER)

        created = self.client.post(
            self.url("employer:job_create"), {"title": "QA Engineer", "description": "Test things."}
        )
        job = Job.objects.get(title="QA Engineer")

        self.assertRedirects(
            created, self.url("employer:job_detail", job.public_id), fetch_redirect_response=False
        )
        self.assertEqual(
            self.client.get(self.url("employer:job_edit", job.public_id)).status_code, 200
        )
        self.assertTrue(
            JobAssignment.objects.filter(job=job, membership=self.members[Role.RECRUITER]).exists()
        )

    def test_a_recruiter_can_edit_the_copy_they_just_duplicated(self):
        # Found by the link sweep after D-13: "Duplicate as draft" landed the
        # recruiter on a copy nobody was assigned to, which they could not edit.
        self.assign(Role.RECRUITER)
        self.as_role(Role.RECRUITER)

        response = self.client.post(
            self.url("employer:job_act", self.job.public_id), {"action": "clone"}
        )
        copy = Job.objects.exclude(pk__in=[self.job.pk, self.other_job.pk]).get()

        self.assertRedirects(
            response, self.url("employer:job_detail", copy.public_id), fetch_redirect_response=False
        )
        self.assertEqual(
            self.client.get(self.url("employer:job_edit", copy.public_id)).status_code, 200
        )

    def test_only_owner_and_admin_change_who_is_on_a_posting(self):
        # D-13: an assignment opens the job's applicants, so a recruiter must not
        # be able to hand it to a colleague or strip one.
        self.assign(Role.RECRUITER)
        team = self.url("employer:job_team", self.job.public_id)
        colleague = self.members[Role.HIRING_MANAGER]

        self.as_role(Role.RECRUITER)
        page = self.client.get(self.url("employer:job_detail", self.job.public_id))
        self.assertNotContains(page, team)
        self.client.post(team, {"membership_id": colleague.pk, "kind": "hiring_manager"})
        self.assertFalse(JobAssignment.objects.filter(job=self.job, membership=colleague).exists())
        self.client.post(
            team,
            {
                "action": "unassign",
                "membership_id": self.members[Role.RECRUITER].pk,
                "kind": "recruiter",
            },
        )
        self.assertTrue(
            JobAssignment.objects.filter(
                job=self.job, membership=self.members[Role.RECRUITER]
            ).exists()
        )

        self.as_role(Role.ADMIN)
        self.assertContains(
            self.client.get(self.url("employer:job_detail", self.job.public_id)), team
        )
        self.client.post(team, {"membership_id": colleague.pk, "kind": "hiring_manager"})
        self.assertTrue(JobAssignment.objects.filter(job=self.job, membership=colleague).exists())

    def test_a_recruiter_cannot_edit_somebody_elses_job(self):
        self.as_role(Role.RECRUITER)

        response = self.client.get(self.url("employer:job_edit", self.job.public_id))

        self.assertEqual(response.status_code, 302)

    def test_a_hiring_manager_sees_only_assigned_jobs(self):
        self.assign(Role.HIRING_MANAGER)
        self.as_role(Role.HIRING_MANAGER)

        listing = self.client.get(self.url("employer:job_list"))
        self.assertContains(listing, "Backend Engineer")
        self.assertNotContains(listing, "Designer")
        self.assertEqual(
            self.client.get(self.url("employer:job_detail", self.other_job.public_id)).status_code,
            404,
        )

    def test_an_interviewer_has_no_jobs_section(self):
        self.as_role(Role.INTERVIEWER)

        self.assertEqual(
            self.client.get(self.url("employer:job_detail", self.job.public_id)).status_code, 403
        )


class InterviewMatrixTests(MatrixFixture):
    def setUp(self):
        super().setUp()
        self.session = InterviewSession.objects.create(
            application=self.application,
            candidate=self.candidate,
            template=live_template(),
            kind="live",
            state=SessionState.SCHEDULED,
            scheduled_at=timezone.now() + dt.timedelta(days=2),
        )
        self.detail_url = self.url("employer:interview_detail", self.session.public_id)

    def test_a_viewer_cannot_cancel_an_interview(self):
        self.as_role(Role.VIEWER)

        self.client.post(self.detail_url, {"action": "cancel", "reason": "no"})

        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.SCHEDULED)

    def test_an_unassigned_interviewer_cannot_open_it(self):
        self.as_role(Role.INTERVIEWER)

        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        listing = self.client.get(self.url("employer:interviews"))
        self.assertNotContains(listing, self.candidate.user.full_name)

    def test_an_owner_keeps_the_record_after_thirty_days(self):
        InterviewSession.objects.filter(pk=self.session.pk).update(
            state=SessionState.COMPLETED, ended_at=timezone.now() - dt.timedelta(days=90)
        )
        self.as_role("owner")

        self.assertEqual(self.client.get(self.detail_url).status_code, 200)

    def test_a_viewer_cannot_publish_interview_times(self):
        from apps.interviews.models import InterviewSlot

        self.as_role(Role.VIEWER)
        start = (timezone.now() + dt.timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
        self.client.post(
            self.url("employer:interview_slots"),
            {"start": start.strftime("%Y-%m-%dT%H:%M"), "minutes": "45"},
        )

        self.assertFalse(InterviewSlot.objects.exists())


class ApplicationStillReachableTests(MatrixFixture):
    """The narrowed scope must not strand the members who own the pipeline."""

    def test_owner_and_admin_read_every_application(self):
        for role in ("owner", Role.ADMIN):
            with self.subTest(role=role):
                self.as_role(role)
                for application in Application.objects.all():
                    self.assertEqual(self.client.get(self.detail(application)).status_code, 200)


class OfferedOnlyWhatOpensTests(MatrixFixture):
    """A page offers a role only the links and forms that role can use.

    Found by `scripts/link_sweep.py`: the applicant board gave a Viewer a bulk
    move bar and links into candidates that answered 403, the overview offered
    "Review" and "Finish it", and the interviews page offered "Publish times".
    """

    def test_the_board_offers_a_viewer_no_bulk_bar_and_no_candidate_links(self):
        self.as_role(Role.VIEWER)

        board = self.client.get(self.url("employer:applicants")).content.decode()

        self.assertIn(self.candidate.user.full_name, board)
        self.assertNotIn('name="application"', board)
        self.assertNotIn(self.detail(), board)
        self.assertNotIn(reverse("employer:pools", args=[self.slug]), board)

    def test_the_board_offers_a_recruiter_both(self):
        self.assign(Role.RECRUITER)
        self.as_role(Role.RECRUITER)

        board = self.client.get(self.url("employer:applicants")).content.decode()

        self.assertIn('name="application"', board)
        self.assertIn(self.detail(), board)

    def test_the_overview_offers_a_viewer_no_review_and_an_interviewer_no_company_links(self):
        self.as_role(Role.VIEWER)
        overview = self.client.get(self.url("employer:overview")).content.decode()
        self.assertNotIn(self.detail(), overview)

        self.as_role(Role.INTERVIEWER)
        overview = self.client.get(self.url("employer:overview")).content.decode()
        for name in ("employer:profile", "employer:verification", "employer:team"):
            self.assertNotIn(reverse(name, args=[self.slug]), overview)

    def test_drafts_listed_on_the_overview_are_ones_the_member_can_finish(self):
        from apps.jobs.tests.factories import create_job as make

        draft = make(self.company, title="Unassigned Draft")
        self.as_role(Role.RECRUITER)
        overview = self.client.get(self.url("employer:overview")).content.decode()
        self.assertNotIn("Unassigned Draft", overview)

        JobAssignment.objects.create(job=draft, membership=self.members[Role.RECRUITER])
        overview = self.client.get(self.url("employer:overview")).content.decode()
        self.assertIn("Unassigned Draft", overview)

    def test_the_interviews_page_offers_a_viewer_no_publish_form(self):
        self.as_role(Role.VIEWER)
        self.assertNotContains(self.client.get(self.url("employer:interviews")), "Publish times")

        self.as_role(Role.INTERVIEWER)
        self.assertContains(self.client.get(self.url("employer:interviews")), "Publish times")
