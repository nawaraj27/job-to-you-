"""
The manual-testing accounts exist, work, and stay one set (DEMO-01).

Run through the real command, the real services and the real sign-in form:
an account that exists but cannot sign in, or a company that exists but its
owner cannot open, is worse than no seed at all for somebody following the
handoff.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import StaffRole, User
from apps.applications.models import Application
from apps.companies.models import Company, CompanyFollow, VerificationState
from apps.core import demo_personas as personas
from apps.interviews.models import InterviewKind, InterviewSession
from apps.jobs.models import Job, JobStatus
from apps.resumes.models import ResumeAnalysis, ResumeDocument


def seed() -> str:
    out = StringIO()
    call_command("seed_demo", "--personas-only", "--force", stdout=out, stderr=StringIO())
    return out.getvalue()


def counts() -> dict[str, int]:
    from apps.companies.models import CompanyMembership
    from apps.interviews.models import InterviewSlot
    from apps.jobs.models import JobAssignment
    from apps.notifications.models import Notification

    emails = [email for _letter, email, _purpose in personas.ACCOUNTS]
    names = [personas.COMPANY, personas.OTHER_COMPANY, personas.PENDING_COMPANY]
    return {
        "memberships": CompanyMembership.objects.count(),
        "assignments": JobAssignment.objects.count(),
        "slots": InterviewSlot.objects.count(),
        "notifications": Notification.objects.count(),
        "users": User.objects.filter(email__in=emails).count(),
        "companies": Company.objects.filter(name__in=names).count(),
        "jobs": Job.objects.filter(company__name__in=names).count(),
        "applications": Application.objects.count(),
        "sessions": InterviewSession.objects.count(),
        "follows": CompanyFollow.objects.count(),
        "cvs": ResumeDocument.objects.count(),
        "analyses": ResumeAnalysis.objects.count(),
        "staff": StaffRole.objects.count(),
    }


class SeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.output = seed()

    def test_the_command_prints_every_credential(self):
        for expected in (
            personas.PASSWORD,
            personas.CANDIDATE_EMAIL,
            personas.EMPLOYER_EMAIL,
            personas.STAFF_EMAIL,
        ):
            self.assertIn(expected, self.output)

    def test_running_it_again_changes_nothing(self):
        before = counts()

        seed()

        self.assertEqual(counts(), before)

    def test_a_published_demo_draft_is_replaced_by_a_fresh_draft(self):
        """DEMO-02: the publish-and-watch-the-follower workflow can be repeated."""
        from apps.jobs.services import lifecycle

        company = Company.objects.get(name=personas.COMPANY)
        draft = Job.objects.get(company=company, title="Data Analyst")
        owner = company.memberships.get(user__email=personas.EMPLOYER_EMAIL)
        lifecycle.publish(job=draft, actor=owner.user, membership=owner)

        seed()
        seed()

        rows = Job.objects.filter(company=company, title="Data Analyst")
        self.assertEqual(
            sorted(rows.values_list("status", flat=True)), [JobStatus.DRAFT, JobStatus.PUBLISHED]
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, JobStatus.PUBLISHED)

    def test_the_employer_company_is_verified_by_a_real_decision(self):
        """DEMO-04: verified through claim-and-decide by the demo moderator."""
        from apps.companies.models import VerificationState

        company = Company.objects.get(name=personas.COMPANY)

        self.assertEqual(company.verification_state, VerificationState.VERIFIED)
        self.assertEqual(company.verification.documents.count(), 2)
        self.assertTrue(
            company.verification.decisions.filter(
                reviewed_by__email=personas.STAFF_EMAIL, state=VerificationState.VERIFIED
            ).exists()
        )

    def test_the_candidate_profile_is_complete(self):
        from apps.accounts.models import User
        from apps.candidates import completeness

        profile = User.objects.get(email=personas.CANDIDATE_EMAIL).candidate_profile

        score, missing = completeness.evaluate(profile)
        self.assertEqual((score, missing), (100, []))

    def test_every_account_signs_in_with_the_documented_password(self):
        for email in (personas.CANDIDATE_EMAIL, personas.EMPLOYER_EMAIL, personas.STAFF_EMAIL):
            with self.subTest(email=email):
                self.assertTrue(self.client.login(username=email, password=personas.PASSWORD))
                self.client.logout()

    def test_the_employer_owns_the_company_and_its_three_different_postings(self):
        company = Company.objects.get(name=personas.COMPANY)
        published = set(
            Job.objects.filter(company=company, status=JobStatus.PUBLISHED).values_list(
                "title", flat=True
            )
        )

        self.assertEqual(company.created_by.email, personas.EMPLOYER_EMAIL)
        # Junior Data Scientist joined the three with the manual-testing pack
        # (ledger §19): a data posting with required and preferred skills.
        self.assertEqual(
            published,
            {
                "Backend Developer",
                "UI/UX Designer",
                "Marketing Specialist",
                "Junior Data Scientist",
            },
        )
        self.assertEqual(
            Job.objects.get(company=company, title="Data Analyst").status, JobStatus.DRAFT
        )

    def test_the_postings_carry_structured_skills(self):
        backend = Job.objects.get(title="Backend Developer", company__name=personas.COMPANY)
        designer = Job.objects.get(title="UI/UX Designer", company__name=personas.COMPANY)

        backend_skills = set(backend.skills.values_list("skill__name", flat=True))
        designer_skills = set(designer.skills.values_list("skill__name", flat=True))
        self.assertIn("Django", backend_skills)
        self.assertIn("Figma", designer_skills)
        self.assertFalse(backend_skills & designer_skills)

    def test_the_candidate_has_a_cv_an_analysis_and_an_application(self):
        candidate = User.objects.get(email=personas.CANDIDATE_EMAIL)
        document = ResumeDocument.objects.get(profile__user=candidate)

        self.assertEqual(document.draft.content["contact"]["name"], "Maya Karki")
        self.assertTrue(ResumeAnalysis.objects.filter(version__document=document).exists())
        application = Application.objects.get(candidate__user=candidate)
        self.assertEqual(application.job.title, "Backend Developer")

    def test_the_candidate_has_a_booked_live_interview_and_an_open_practice(self):
        candidate = User.objects.get(email=personas.CANDIDATE_EMAIL)
        sessions = InterviewSession.objects.filter(candidate__user=candidate)

        self.assertTrue(
            sessions.filter(kind=InterviewKind.LIVE, application__isnull=False).exists()
        )
        practice = sessions.get(application__isnull=True)
        self.assertEqual(practice.plan.get("job_title"), "Backend Developer")

    def test_a_live_interview_that_has_passed_is_moved_to_an_upcoming_day(self):
        # DEMO-06: the booking was made once and never moved, so three days
        # after the first seed the demo showed a "Scheduled" interview in the
        # past. Re-seeding moves it through the real reschedule path.
        from django.db.models import F
        from django.utils import timezone

        from apps.interviews.models import InterviewSlot, SessionState

        live = InterviewSession.objects.get(kind=InterviewKind.LIVE, state=SessionState.SCHEDULED)
        InterviewSlot.objects.filter(session=live).update(
            starts_at=F("starts_at") - timezone.timedelta(days=10),
            ends_at=F("ends_at") - timezone.timedelta(days=10),
        )

        seed()

        live.refresh_from_db()
        self.assertEqual(live.state, SessionState.CANCELLED)
        upcoming = InterviewSession.objects.get(
            kind=InterviewKind.LIVE, state=SessionState.SCHEDULED
        )
        self.assertEqual(upcoming.rescheduled_from, live)
        self.assertGreater(upcoming.slot.starts_at, timezone.now())
        before = counts()

        seed()

        self.assertEqual(counts(), before)

    def test_the_candidate_follows_one_company_and_not_the_other(self):
        candidate = User.objects.get(email=personas.CANDIDATE_EMAIL)
        followed = set(
            CompanyFollow.objects.filter(user=candidate).values_list("company__name", flat=True)
        )

        self.assertEqual(followed, {personas.COMPANY})

    def test_the_pending_company_is_submitted_with_one_certificate(self):
        company = Company.objects.get(name=personas.PENDING_COMPANY)

        self.assertEqual(company.verification.state, VerificationState.PENDING)
        self.assertEqual(
            list(company.verification.documents.values_list("kind", flat=True)), ["registration"]
        )

    def test_the_staff_account_is_a_moderator_not_a_django_superuser(self):
        staff = User.objects.get(email=personas.STAFF_EMAIL)

        self.assertEqual(staff.staff_role.level, StaffRole.Level.MODERATOR)
        self.assertFalse(staff.is_superuser)
        self.assertFalse(staff.is_staff)

    def test_the_staff_account_reaches_the_queue_and_sees_the_gap(self):
        self.client.login(username=personas.STAFF_EMAIL, password=personas.PASSWORD)

        response = self.client.get(reverse("staff:verification"))

        self.assertContains(response, personas.PENDING_COMPANY)
        self.assertContains(response, "Not sent:")

    def test_the_candidate_cannot_reach_the_staff_queue(self):
        self.client.login(username=personas.CANDIDATE_EMAIL, password=personas.PASSWORD)

        self.assertEqual(self.client.get(reverse("staff:verification")).status_code, 404)

    def test_personas_only_refuses_reset(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("seed_demo", "--personas-only", "--reset", "--force", stdout=StringIO())


class CandidatePlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def test_the_candidate_can_run_another_analysis_for_a_target_job(self):
        """The seed spends one analysis; a tester still needs to run theirs."""
        from apps.billing.services import entitlements
        from apps.resumes.services import analysis as analysis_service

        candidate = User.objects.get(email=personas.CANDIDATE_EMAIL)
        profile = candidate.candidate_profile
        self.assertIsNotNone(entitlements.live_subscription(profile))
        document = ResumeDocument.objects.get(profile=profile)
        job = Job.objects.get(title="Backend Developer", company__name=personas.COMPANY)

        analysis = analysis_service.analyse(
            document=document, actor=candidate, job=job, defer_ai=True
        )

        self.assertEqual(analysis.job_id, job.pk)


class ManualTestingPackTests(TestCase):
    """The accounts the manual-testing pack documents (ledger §19)."""

    @classmethod
    def setUpTestData(cls):
        seed()

    def signed_in(self, email):
        self.client.force_login(User.objects.get(email=email))

    def application(self, email, title):
        return Application.objects.get(candidate__user__email=email, job__title=title)

    def test_every_documented_account_exists_confirmed_and_signs_in(self):
        from django.contrib.auth import authenticate

        for _letter, email, _purpose in personas.ACCOUNTS:
            with self.subTest(email=email):
                user = User.objects.get(email=email)
                self.assertTrue(
                    user.email_addresses.filter(is_primary=True, verified_at__isnull=False).exists()
                )
                self.assertEqual(authenticate(username=email, password=personas.PASSWORD), user)

    def test_the_admin_and_recruiter_joined_summit_by_invitation(self):
        from apps.companies.models import CompanyMembership

        summit = Company.objects.get(name=personas.COMPANY)
        roles = dict(
            CompanyMembership.objects.filter(company=summit).values_list("user__email", "role")
        )
        self.assertEqual(roles[personas.EMPLOYER_EMAIL], "owner")
        self.assertEqual(roles[personas.ADMIN_EMAIL], "admin")
        self.assertEqual(roles[personas.RECRUITER_EMAIL], "recruiter")

    def test_the_recruiter_is_assigned_to_backend_developer_only(self):
        from apps.jobs.models import JobAssignment

        assigned = set(
            JobAssignment.objects.filter(
                membership__user__email=personas.RECRUITER_EMAIL
            ).values_list("job__title", flat=True)
        )
        self.assertEqual(assigned, {"Backend Developer"})

    def test_the_recruiter_sees_the_assigned_applicant_and_not_the_others(self):
        summit = Company.objects.get(name=personas.COMPANY)
        self.signed_in(personas.RECRUITER_EMAIL)

        board = self.client.get(reverse("employer:applicants", args=[summit.slug]))
        self.assertContains(board, "Maya")
        self.assertNotContains(board, "Arjun")
        self.assertNotContains(board, "Sita")
        arjun = self.application(personas.INTERVIEW_CANDIDATE_EMAIL, "UI/UX Designer")
        response = self.client.get(
            reverse("employer:application_detail", args=[summit.slug, arjun.public_id])
        )
        self.assertIn(response.status_code, (403, 404))

    def test_the_interview_candidate_can_book_a_time(self):
        arjun = self.application(personas.INTERVIEW_CANDIDATE_EMAIL, "UI/UX Designer")
        self.assertEqual(arjun.stage.key, "interview")
        self.signed_in(personas.INTERVIEW_CANDIDATE_EMAIL)

        page = self.client.get(reverse("applications:detail", args=[arjun.public_id]))
        self.assertContains(page, "Pick a time")
        booking = self.client.get(reverse("interviews:book", args=[arjun.public_id]))
        self.assertContains(booking, "Book your interview")

    def test_the_interview_candidate_has_a_practice_interview_for_the_job(self):
        arjun = User.objects.get(email=personas.INTERVIEW_CANDIDATE_EMAIL)
        sessions = InterviewSession.objects.filter(candidate__user=arjun, application__isnull=True)
        self.assertTrue(any(s.plan.get("job_title") == "UI/UX Designer" for s in sessions))

    def test_the_second_candidate_is_separate_and_follows_the_other_company(self):
        sita = User.objects.get(email=personas.SECOND_CANDIDATE_EMAIL)
        titles = set(
            Application.objects.filter(candidate__user=sita).values_list("job__title", flat=True)
        )
        self.assertEqual(titles, {"Junior Data Scientist", "Operations Coordinator"})
        followed = set(
            CompanyFollow.objects.filter(user=sita).values_list("company__name", flat=True)
        )
        self.assertEqual(followed, {personas.OTHER_COMPANY})

    def test_the_second_company_cannot_open_the_first_companys_applicant(self):
        summit = Company.objects.get(name=personas.COMPANY)
        maya = self.application(personas.CANDIDATE_EMAIL, "Backend Developer")
        self.signed_in(personas.OTHER_EMPLOYER_EMAIL)

        response = self.client.get(
            reverse("employer:application_detail", args=[summit.slug, maya.public_id])
        )
        self.assertIn(response.status_code, (403, 404))

    def test_the_new_candidate_has_nothing_yet(self):
        ramesh = User.objects.get(email=personas.NEW_CANDIDATE_EMAIL)
        self.assertFalse(ResumeDocument.objects.filter(profile__user=ramesh).exists())
        self.assertFalse(Application.objects.filter(candidate__user=ramesh).exists())

    def test_the_data_posting_has_required_and_preferred_skills(self):
        job = Job.objects.get(company__name=personas.COMPANY, title="Junior Data Scientist")
        self.assertEqual(job.status, JobStatus.PUBLISHED)
        required = set(
            job.skills.filter(requirement="required").values_list("skill__name", flat=True)
        )
        preferred = set(
            job.skills.filter(requirement="preferred").values_list("skill__name", flat=True)
        )
        self.assertEqual(required, {"Python", "SQL", "Machine learning"})
        self.assertEqual(preferred, {"pandas", "Tableau"})
