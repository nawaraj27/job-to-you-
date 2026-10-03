"""
Company and account moderation (SRS §2.3 platform roles, §2.6 suspension).

The whole-site loop found no path for a moderator to do the three things §2.3
says a moderator does — unpublish a job, suspend a company, suspend an account —
and a suspension that left scheduled interviews standing, let candidates keep
messaging the company, and told the company nothing.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import EmailAddress, StaffRole
from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.candidates.tests.factories import create_profile
from apps.companies.models import CompanyState
from apps.companies.tests.factories import company_with_owner
from apps.interviews.models import InterviewSession, SessionState
from apps.interviews.tests.factories import live_template
from apps.jobs.models import JobStatus
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for
from apps.notifications.models import OutboxEvent


def staff_user(level: str):
    user = create_user()
    StaffRole.objects.create(user=user, level=level)
    return user


class ModerationFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company, title="Backend Engineer"))
        self.moderator = staff_user("moderator")
        self.support = staff_user("support")
        self.company_url = reverse("staff:company", args=[self.company.slug])


class AccessTests(ModerationFixture):
    def test_support_reads_but_cannot_act(self):
        self.client.force_login(self.support)

        self.assertEqual(self.client.get(reverse("staff:companies")).status_code, 200)
        self.assertEqual(self.client.get(self.company_url).status_code, 200)
        self.client.post(self.company_url, {"action": "suspend", "reason": "spam"})

        self.company.refresh_from_db()
        self.assertEqual(self.company.state, CompanyState.ACTIVE)

    def test_non_staff_do_not_see_the_screens_exist(self):
        for user in (self.owner, create_user()):
            self.client.force_login(user)
            for url in (
                reverse("staff:companies"),
                self.company_url,
                reverse("staff:people"),
                reverse("staff:person", args=[self.owner.pk]),
            ):
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 404)

    def test_people_search_lists_nobody_until_asked(self):
        self.client.force_login(self.support)

        empty = self.client.get(reverse("staff:people"))
        found = self.client.get(reverse("staff:people"), {"q": self.owner.email})

        self.assertNotContains(empty, self.owner.email)
        self.assertContains(found, self.owner.email)


class CompanySuspensionTests(ModerationFixture):
    def test_suspension_needs_a_reason(self):
        self.client.force_login(self.moderator)

        self.client.post(self.company_url, {"action": "suspend", "reason": "  "})

        self.company.refresh_from_db()
        self.assertEqual(self.company.state, CompanyState.ACTIVE)

    def test_suspension_cascades_and_tells_the_company(self):
        candidate = create_profile()
        application = application_for(candidate, self.job)
        session = InterviewSession.objects.create(
            application=application,
            candidate=candidate,
            template=live_template(),
            kind="live",
            state=SessionState.SCHEDULED,
            scheduled_at=timezone.now() + dt.timedelta(days=2),
        )
        self.client.force_login(self.moderator)

        self.client.post(self.company_url, {"action": "suspend", "reason": "Fraudulent postings"})

        self.company.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.company.state, CompanyState.SUSPENDED)
        self.assertEqual(session.state, SessionState.CANCELLED)
        self.assertTrue(OutboxEvent.objects.filter(type="interview.cancelled").exists())
        suspended = OutboxEvent.objects.get(type="company.suspended")
        self.assertEqual(suspended.payload["reason"], "Fraudulent postings")
        audit = AuditLog.objects.get(verb="company.suspended")
        self.assertEqual(audit.actor, self.moderator)
        self.assertEqual(audit.metadata["interviews_cancelled"], 1)

        # The public side and the team are closed.
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("jobs:detail", args=[self.job.slug])).status_code, 404
        )
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse("employer:overview", args=[self.company.slug])).status_code, 404
        )

    def test_a_candidate_cannot_message_a_suspended_company(self):
        from apps.common.errors import NotAllowed
        from apps.companies.services import company as company_service
        from apps.messaging.models import Conversation
        from apps.messaging.services import conversations

        candidate = create_profile()
        application_for(candidate, self.job)
        conversation = Conversation.objects.create(company=self.company, candidate=candidate)
        company_service.suspend_company(actor=self.moderator, company=self.company, reason="x")
        conversation.refresh_from_db()

        with self.assertRaises(NotAllowed):
            conversations._require_sendable(conversation, "candidate")

    def test_reinstating_brings_the_postings_back(self):
        from apps.companies.services import company as company_service

        company_service.suspend_company(actor=self.moderator, company=self.company, reason="x")
        self.client.force_login(self.moderator)

        self.client.post(self.company_url, {"action": "reinstate", "reason": "Appeal upheld"})

        self.company.refresh_from_db()
        self.assertEqual(self.company.state, CompanyState.ACTIVE)
        self.assertTrue(AuditLog.objects.filter(verb="company.reinstated").exists())
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("jobs:detail", args=[self.job.slug])).status_code, 200
        )


class TakeDownTests(ModerationFixture):
    def test_a_moderator_takes_a_posting_down_with_a_reason(self):
        self.client.force_login(self.moderator)

        self.client.post(
            self.company_url,
            {"action": "take_down", "job": str(self.job.public_id), "reason": "Misleading salary"},
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.DRAFT)
        event = OutboxEvent.objects.get(type="job.taken_down")
        self.assertEqual(event.payload["reason"], "Misleading salary")

        # The company's editors are told, and the notice opens the posting.
        from apps.notifications.models import Notification
        from apps.notifications.services import fanout, outbox

        for _ in range(3):
            outbox.drain(100)
            fanout.run_due(limit=100)
        notice = Notification.objects.get(recipient=self.owner, kind="job.taken_down")
        self.client.force_login(self.owner)
        opened = self.client.get(reverse("notifications:open", args=[notice.public_id]))
        self.assertEqual(
            opened["Location"],
            reverse("employer:job_detail", args=[self.company.slug, self.job.public_id]),
        )
        self.assertEqual(self.client.get(opened["Location"]).status_code, 200)

    def test_a_posting_from_another_company_cannot_be_reached_through_this_page(self):
        other_owner = create_profile().user
        other_company, _ = company_with_owner(other_owner)
        other_job = publish(create_job(other_company))
        self.client.force_login(self.moderator)

        self.client.post(
            self.company_url,
            {"action": "take_down", "job": str(other_job.public_id), "reason": "x"},
        )

        other_job.refresh_from_db()
        self.assertEqual(other_job.status, JobStatus.PUBLISHED)


class AccountModerationTests(ModerationFixture):
    def test_suspending_signs_them_out_and_stops_sign_in(self):
        person = create_user()
        theirs = self.client_class()
        theirs.post(
            reverse("accounts:login"),
            {"username": person.email, "password": "test-password-not-a-secret"},
        )
        self.assertEqual(theirs.get(reverse("settings:security")).status_code, 200)

        self.client.force_login(self.moderator)
        self.client.post(
            reverse("staff:person", args=[person.pk]), {"action": "suspend", "reason": "Spam"}
        )

        person.refresh_from_db()
        self.assertFalse(person.is_active)
        self.assertEqual(theirs.get(reverse("settings:security")).status_code, 302)
        self.assertTrue(
            AuditLog.objects.filter(verb="account.suspended", target_id=str(person.pk)).exists()
        )

    def test_a_moderator_cannot_suspend_staff_or_themselves(self):
        self.client.force_login(self.moderator)
        for target in (self.support, self.moderator):
            with self.subTest(target=target.email):
                self.client.post(
                    reverse("staff:person", args=[target.pk]), {"action": "suspend", "reason": "x"}
                )
                target.refresh_from_db()
                self.assertTrue(target.is_active)

    def test_support_resends_a_confirmation_link_but_cannot_suspend(self):
        from django.core import mail

        person = create_user()
        EmailAddress.objects.create(user=person, email=person.email, is_primary=True)
        self.client.force_login(self.support)

        self.client.post(
            reverse("staff:person", args=[person.pk]), {"action": "resend_verification"}
        )
        self.client.post(
            reverse("staff:person", args=[person.pk]), {"action": "suspend", "reason": "x"}
        )

        self.assertEqual(len(mail.outbox), 1)
        person.refresh_from_db()
        self.assertTrue(person.is_active)

    def test_a_malformed_person_id_is_a_404(self):
        self.client.force_login(self.support)
        self.assertEqual(self.client.get("/staff/people/999999999/").status_code, 404)
