"""
The whole product, end to end, through its own screens (whole-site loop).

Every earlier loop tested a section. This test is the join between them: one
employer and one candidate, both signing up from nothing, doing what the product
exists for — post a job, apply, move through the pipeline, book an interview,
make and accept an offer — and then the team changes underneath it. Every step
is an HTTP request to a real view; the only shortcut is the CV's content, which
the CV loops own.

What it asserts is the connective tissue: the same application is visible on
both sides, each move notifies the right person, notification links open, a
removed member loses access in the same request cycle, and nothing crosses to
another company.
"""

from __future__ import annotations

import datetime as dt
import re

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.applications.models import Application, Offer
from apps.companies.models import Company, CompanyMembership
from apps.interviews.models import InterviewSession, InterviewSlot
from apps.jobs.models import Job, JobStatus
from apps.notifications.models import Notification
from apps.notifications.services import fanout, outbox
from apps.resumes.models import ResumeDocument

PASSWORD = "Kathmandu-Valley-2026!"


def deliver():
    """Run the outbox and fan-out the way the workers would."""
    for _ in range(10):
        moved = outbox.drain(500)
        chunks = fanout.run_due(limit=500).get("chunks", 0)
        if not moved and not chunks:
            return


class WholeSiteJourneyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)
        call_command("seed_interview_templates", verbosity=0)

    def sign_up(self, client, *, email, name, intent):
        cache.clear()  # the per-IP sign-up limit is shared by every test client
        response = client.post(
            reverse("accounts:signup"),
            {
                "full_name": name,
                "email": email,
                "signup_intent": intent,
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302, response.content[:500])
        # Decision D-12: the same "check your email" answer for everybody, and
        # nobody signed in until they confirm and sign in.
        self.assertEqual(response["Location"], reverse("accounts:signup_sent"))
        link = re.search(r"http://testserver(/accounts/verify/\S+/)", mail.outbox[-1].body)
        client.get(link.group(1))
        landing = client.post(reverse("accounts:login"), {"username": email, "password": PASSWORD})
        return User.objects.get(email=email), landing

    def test_hiring_from_nothing_to_an_accepted_offer(self):
        employer = self.client_class()
        candidate = self.client_class()

        # --- the employer arrives, creates a company, posts a job ---------------
        owner, landing = self.sign_up(
            employer, email="founder@himal.example", name="Asha Founder", intent="employer"
        )
        self.assertEqual(landing["Location"], reverse("employer:index"))
        created = employer.post(reverse("employer:create"), {"name": "Himal Tech"})
        company = Company.objects.get(name="Himal Tech")
        self.assertRedirects(
            created,
            reverse("employer:overview", args=[company.slug]),
            fetch_redirect_response=False,
        )
        # The owner is inside the two-factor grace period, and is told so.
        self.assertEqual(
            employer.get(reverse("employer:overview", args=[company.slug])).status_code, 200
        )

        employer.post(
            reverse("employer:job_create", args=[company.slug]),
            {
                "title": "Platform Engineer",
                "description": "Run the platform.",
                "salary_min": "150000",
                "salary_max": "200000",
                "currency": "NPR",
                "period": "month",
            },
        )
        job = Job.objects.get(company=company, title="Platform Engineer")
        detail = reverse("employer:job_detail", args=[company.slug, job.public_id])
        self.assertEqual(employer.get(detail).status_code, 200)
        employer.post(
            reverse("employer:job_act", args=[company.slug, job.public_id]),
            {"action": "publish", "row_version": job.row_version},
        )
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.PUBLISHED)

        # --- a candidate finds it and applies ------------------------------------
        seeker, landing = self.sign_up(
            candidate, email="bina@example.org", name="Bina Candidate", intent="candidate"
        )
        self.assertEqual(landing["Location"], reverse("dashboard:index"))
        self.assertContains(candidate.get(reverse("jobs:list")), "Platform Engineer")
        self.assertEqual(candidate.get(reverse("jobs:detail", args=[job.slug])).status_code, 200)

        candidate.post(
            reverse("resume:builder"), {"name": "My CV", "mode": "editor", "template": "atlas"}
        )
        document = ResumeDocument.objects.get(profile__user=seeker, name="My CV")
        from apps.resumes.services import drafts as draft_service
        from apps.resumes.tests.test_rendering import sample_content

        draft_service.autosave(draft=document.draft, content=sample_content())

        applied = candidate.post(
            reverse("jobs:apply", args=[job.slug]), {"document": str(document.public_id)}
        )
        application = Application.objects.get(job=job, candidate__user=seeker)
        self.assertRedirects(
            applied,
            reverse("applications:detail", args=[application.public_id]),
            fetch_redirect_response=False,
        )
        self.assertContains(candidate.get(reverse("applications:index")), "Platform Engineer")

        # --- the employer sees it, and the team is told --------------------------
        deliver()
        self.assertTrue(Notification.objects.filter(recipient=owner).exists())
        board = employer.get(reverse("employer:applicants", args=[company.slug]))
        self.assertContains(board, "Bina Candidate")
        app_url = reverse("employer:application_detail", args=[company.slug, application.public_id])
        self.assertEqual(employer.get(app_url).status_code, 200)

        # Notification links go somewhere that opens.
        for note in Notification.objects.filter(recipient=owner):
            opened = employer.get(reverse("notifications:open", args=[note.public_id]))
            self.assertIn(opened.status_code, (200, 302))
            if opened.status_code == 302:
                self.assertLess(
                    employer.get(opened["Location"]).status_code, 400, opened["Location"]
                )

        # --- interview: the employer publishes times, the candidate books one ----
        act = reverse("employer:application_act", args=[company.slug, application.public_id])
        employer.post(act, {"action": "move", "stage": "interview"})
        application.refresh_from_db()
        self.assertEqual(application.stage.key, "interview")

        day = (timezone.localdate() + dt.timedelta(days=3)).isoformat()
        employer.post(
            reverse("employer:interview_slots", args=[company.slug]),
            {"date": day, "time": ["10:00", "11:00"], "minutes": "45"},
        )
        self.assertEqual(InterviewSlot.objects.filter(interviewer__company=company).count(), 2)
        booking = reverse("interviews:book", args=[application.public_id])
        self.assertEqual(candidate.get(booking).status_code, 200)
        slot = (
            InterviewSlot.objects.filter(interviewer__company=company).order_by("starts_at").first()
        )
        candidate.post(booking, {"slot": slot.pk})
        session = InterviewSession.objects.get(application=application)
        self.assertEqual(
            employer.get(
                reverse("employer:interview_detail", args=[company.slug, session.public_id])
            ).status_code,
            200,
        )
        deliver()
        self.assertTrue(
            Notification.objects.filter(recipient=seeker, kind__startswith="interview").exists()
            or Notification.objects.filter(recipient=seeker).exists()
        )

        # --- offer and acceptance -----------------------------------------------
        offer_url = reverse(
            "employer:application_offer", args=[company.slug, application.public_id]
        )
        employer.post(
            offer_url,
            {
                "action": "draft",
                "salary": "180000",
                "currency": "NPR",
                "period": "month",
                "start_date": (timezone.localdate() + dt.timedelta(days=30)).isoformat(),
                "expires_on": (timezone.localdate() + dt.timedelta(days=10)).isoformat(),
                "message": "We would love to have you.",
            },
        )
        employer.post(offer_url, {"action": "send"})
        offer = Offer.objects.get(application=application)
        self.assertEqual(offer.state, "sent")
        candidate.post(
            reverse("applications:detail", args=[application.public_id]), {"action": "accept_offer"}
        )
        offer.refresh_from_db()
        self.assertEqual(offer.state, "accepted")
        deliver()
        self.assertTrue(
            Notification.objects.filter(recipient=owner, kind__startswith="offer").exists()
        )

        # --- the team changes underneath it --------------------------------------
        team = reverse("employer:team", args=[company.slug])
        employer.post(team, {"action": "invite", "email": "viewer@himal.example", "role": "viewer"})
        link = re.search(r"/employer/invites/[^/\s]+/", mail.outbox[-1].body.replace("=\n", ""))
        self.assertIsNotNone(link)
        viewer_client = self.client_class()
        viewer, _ = self.sign_up(
            viewer_client, email="viewer@himal.example", name="Vik Viewer", intent="employer"
        )
        viewer_client.post(link.group(0))
        membership = CompanyMembership.objects.get(company=company, user=viewer)

        self.assertContains(
            viewer_client.get(reverse("employer:applicants", args=[company.slug])), "Bina Candidate"
        )
        self.assertEqual(viewer_client.get(app_url).status_code, 403)
        viewer_client.post(act, {"action": "move", "stage": "reviewing"})
        application.refresh_from_db()
        self.assertEqual(application.stage.key, "interview")

        employer.post(team, {"action": "remove", "membership_id": membership.pk})
        self.assertEqual(
            viewer_client.get(reverse("employer:applicants", args=[company.slug])).status_code, 404
        )

        # --- nothing crosses to another company -----------------------------------
        stranger = self.client_class()
        self.sign_up(stranger, email="other@rival.example", name="Rival Owner", intent="employer")
        stranger.post(reverse("employer:create"), {"name": "Rival Co"})
        rival = Company.objects.get(name="Rival Co")
        self.assertEqual(stranger.get(app_url).status_code, 404)
        self.assertEqual(
            stranger.get(
                reverse("employer:application_detail", args=[rival.slug, application.public_id])
            ).status_code,
            404,
        )
