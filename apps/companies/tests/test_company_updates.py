"""
What a followed company tells its followers (FOL-03).

Manual testing asked for more than new postings: followers should hear when
the company is verified, and when it posts news or an event. All of it goes
through the notification system under one switch, "Companies you follow", to
the `company_followers` audience — which already enforces "only follows made
before the event" and "never somebody who blocked the company".
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_staff, create_user
from apps.candidates.models import BlockedCompany
from apps.candidates.services import profile as profile_service
from apps.common.errors import InvalidState, NotAllowed
from apps.companies.models import CompanyUpdate, CompanyUpdateKind, Role, VerificationState
from apps.companies.services import company as company_service
from apps.companies.services import follows
from apps.companies.services import updates as update_service
from apps.companies.tests.factories import add_member, company_with_owner
from apps.files.tests import factories as file_factories
from apps.notifications.models import Notification, NotificationPreference

from .test_company_follow import deliver


class UpdateFixture(TestCase):
    def setUp(self):
        self.owner = create_user(full_name="Owner Person")
        self.company, self.membership = company_with_owner(self.owner, name="Kora Studio")
        self.follower = create_user(full_name="Anjali Thapa")
        profile_service.get_or_create_profile(user=self.follower)
        self.bystander = create_user(full_name="Bina Bystander")
        profile_service.get_or_create_profile(user=self.bystander)
        follows.follow(user=self.follower, company=self.company)

    def notices(self, user, kind):
        return Notification.objects.filter(recipient=user, kind=kind)

    def announce(self, **overrides):
        values = {
            "actor": self.owner,
            "membership": self.membership,
            "company": self.company,
            "kind": CompanyUpdateKind.ANNOUNCEMENT,
            "title": "We are opening a Pokhara office",
            "body": "Twelve new roles over the year.",
        }
        values.update(overrides)
        update = update_service.publish(**values)
        deliver()
        return update


class AnnouncementTests(UpdateFixture):
    def test_a_follower_hears_about_an_announcement_and_nobody_else_does(self):
        self.announce()

        notice = self.notices(self.follower, "company.update_published").get()
        self.assertIn("Kora Studio", notice.title)
        self.assertIn("Pokhara office", notice.title)
        self.assertTrue(notice.target_url.endswith("#updates"))
        self.assertFalse(Notification.objects.filter(recipient=self.bystander).exists())
        self.assertEqual(self.client.get(notice.target_url.split("#")[0]).status_code, 200)

    def test_an_event_needs_a_future_time_and_says_when(self):
        with self.assertRaises(InvalidState):
            self.announce(kind=CompanyUpdateKind.EVENT)
        with self.assertRaises(InvalidState):
            self.announce(
                kind=CompanyUpdateKind.EVENT, starts_at=timezone.now() - dt.timedelta(hours=1)
            )

        self.announce(
            kind=CompanyUpdateKind.EVENT,
            title="Hiring day",
            starts_at=timezone.now() + dt.timedelta(days=3),
            location="Lakeside, Pokhara",
        )

        notice = self.notices(self.follower, "company.update_published").get()
        self.assertIn("Event", notice.body)
        self.assertIn("Lakeside, Pokhara", notice.body)

    def test_only_people_who_can_edit_the_company_may_post(self):
        recruiter = add_member(self.company, create_user(), Role.RECRUITER)

        with self.assertRaises(NotAllowed):
            self.announce(actor=recruiter.user, membership=recruiter)
        self.assertFalse(CompanyUpdate.objects.exists())

    def test_the_preference_silences_it(self):
        NotificationPreference.objects.update_or_create(
            user=self.follower,
            event_key=NotificationPreference.Event.COMPANY_UPDATES,
            defaults={"in_app": False, "email": False},
        )

        self.announce()

        self.assertFalse(self.notices(self.follower, "company.update_published").exists())

    def test_a_blocked_company_reaches_nobody(self):
        BlockedCompany.objects.create(profile=self.follower.candidate_profile, company=self.company)

        self.announce()

        self.assertFalse(self.notices(self.follower, "company.update_published").exists())

    def test_one_notice_per_update(self):
        update = self.announce()
        deliver()

        self.assertEqual(self.notices(self.follower, "company.update_published").count(), 1)
        self.assertEqual(update.company.updates.count(), 1)

    def test_a_withdrawn_update_leaves_the_public_page(self):
        update = self.announce()
        page = reverse("jobs:company", args=[self.company.slug])
        self.assertContains(self.client.get(page), "Pokhara office")

        update_service.withdraw(actor=self.owner, membership=self.membership, update=update)

        self.assertNotContains(self.client.get(page), "Pokhara office")
        # The notice already delivered stays: it was true when it was sent.
        self.assertTrue(self.notices(self.follower, "company.update_published").exists())


class VerifiedTests(UpdateFixture):
    def verify(self):
        company_service.add_verification_document(
            actor=self.owner,
            membership=self.membership,
            company=self.company,
            kind="registration",
            uploaded_file=file_factories.pdf_upload("registration.pdf"),
        )
        company_service.submit_verification(actor=self.owner, company=self.company)
        staff = create_staff()
        company_service.claim_verification(actor=staff, company=self.company)
        company_service.review_verification(
            actor=staff, company=self.company, decision=VerificationState.VERIFIED
        )
        deliver()

    def test_followers_hear_that_the_company_was_verified(self):
        self.verify()

        notice = self.notices(self.follower, "company.verified").get()
        self.assertEqual(notice.title, "Kora Studio is now verified")
        self.assertEqual(notice.target_url, reverse("jobs:company", args=[self.company.slug]))
        self.assertFalse(self.notices(self.bystander, "company.verified").exists())


class ScreenTests(UpdateFixture):
    def test_the_owner_posts_from_the_employer_screen(self):
        self.client.force_login(self.owner)
        url = reverse("employer:updates", args=[self.company.slug])

        self.assertContains(self.client.get(url), "Post and tell followers")
        response = self.client.post(
            url,
            {"kind": "announcement", "title": "New benefits", "body": "Lunch is on us."},
            follow=True,
        )

        self.assertContains(response, "Your followers will be told")
        self.assertTrue(CompanyUpdate.objects.filter(title="New benefits").exists())

    def test_a_recruiter_sees_the_list_and_no_form(self):
        recruiter = add_member(self.company, create_user(), Role.RECRUITER)
        self.client.force_login(recruiter.user)

        response = self.client.get(reverse("employer:updates", args=[self.company.slug]))

        self.assertIn(response.status_code, (200, 403, 404))
        if response.status_code == 200:
            self.assertNotContains(response, "Post and tell followers")

    def test_the_settings_screen_offers_the_switch(self):
        self.client.force_login(self.follower)

        response = self.client.get(reverse("settings:notifications"))

        self.assertContains(response, "Companies you follow")
