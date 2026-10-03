"""
Offer and closed-posting defects reproduced on the development database.

  * an offer could be drafted and sent with a start date in the past, and the
    candidate read "starting 8 Sep 2026" five days after it;
  * after a candidate accepted, a second offer could still be drafted, so
    "what did we agree" had two answers;
  * the team was told only "The candidate has responded to your offer" — not
    who, for which role, or whether they said yes;
  * after acceptance nothing told the employer the hire still had to be
    recorded, and the application sat at "Interview";
  * a candidate whose posting had closed saw "New" with no sign it had closed.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.applications.models import OfferState
from apps.applications.services import offers as offer_service
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.companies.tests.factories import company_with_owner
from apps.jobs.services import lifecycle
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import OutboxEvent


class OfferFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company, title="Backend Engineer"))
        self.candidate = create_profile()
        self.application = submit(job=self.job, profile=self.candidate)

    def draft(self, **fields):
        return offer_service.draft_offer(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            salary=fields.pop("salary", "150000"),
            currency="NPR",
            period="month",
            **fields,
        )

    def accepted(self):
        offer = self.draft(start_date=timezone.localdate() + dt.timedelta(days=30))
        offer_service.send(offer=offer, actor=self.owner, membership=self.membership)
        return offer_service.respond(offer=offer, actor=self.candidate.user, accept=True)


class StartDateTests(OfferFixture):
    def test_a_start_date_in_the_past_is_refused(self):
        with self.assertRaisesMessage(InvalidState, "start date"):
            self.draft(start_date=timezone.localdate() - dt.timedelta(days=5))

    def test_today_and_later_are_accepted(self):
        offer = self.draft(start_date=timezone.localdate())
        self.assertEqual(offer.state, OfferState.DRAFT)

    def test_the_employer_screen_says_why(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("employer:application_offer", args=["acme-co", self.application.public_id]),
            {
                "action": "draft",
                "salary": "150000",
                "period": "month",
                "start_date": (timezone.localdate() - dt.timedelta(days=5)).isoformat(),
            },
            follow=True,
        )
        self.assertContains(response, "start date")
        self.assertFalse(self.application.offers.exists())


class AfterAcceptanceTests(OfferFixture):
    def test_no_second_offer_after_one_was_accepted(self):
        self.accepted()
        with self.assertRaisesMessage(InvalidState, "accepted"):
            self.draft(start_date=timezone.localdate() + dt.timedelta(days=40))

    def test_the_team_notification_names_the_role_and_the_answer(self):
        from apps.notifications.events import spec_for
        from apps.notifications.services.channels import render

        self.accepted()
        event = OutboxEvent.objects.filter(type="offer.decided").latest("pk")
        title, body = render(spec_for(event.type), event.payload)

        self.assertIn("Backend Engineer", title + body)
        self.assertIn("accepted", (title + body).lower())

    def test_a_declined_offer_says_declined(self):
        from apps.notifications.events import spec_for
        from apps.notifications.services.channels import render

        offer = self.draft(start_date=timezone.localdate() + dt.timedelta(days=30))
        offer_service.send(offer=offer, actor=self.owner, membership=self.membership)
        offer_service.respond(offer=offer, actor=self.candidate.user, accept=False, reason="Pay")
        event = OutboxEvent.objects.filter(type="offer.decided").latest("pk")
        title, body = render(spec_for(event.type), event.payload)

        self.assertIn("declined", (title + body).lower())

    def test_the_employer_page_prompts_to_record_the_hire(self):
        self.accepted()
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("employer:application_detail", args=["acme-co", self.application.public_id])
        )
        self.assertContains(response, "accepted your offer")
        self.assertContains(response, "Hired")


class ClosedPostingTests(OfferFixture):
    def test_a_candidate_is_told_the_posting_closed(self):
        lifecycle.close(job=self.job, actor=self.owner, membership=self.membership, reason="Filled")
        self.client.force_login(self.candidate.user)

        response = self.client.get(
            reverse("applications:detail", args=[self.application.public_id])
        )

        self.assertContains(response, "no longer accepting applications")

    def test_an_open_posting_says_nothing_about_closing(self):
        self.client.force_login(self.candidate.user)
        response = self.client.get(
            reverse("applications:detail", args=[self.application.public_id])
        )
        self.assertNotContains(response, "no longer accepting applications")


class WithdrawnApplicationScreenTests(OfferFixture):
    def test_a_withdrawn_application_offers_no_move_form(self):
        from apps.applications.services import apply as apply_service

        apply_service.withdraw(application=self.application, actor=self.candidate.user)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("employer:application_detail", args=["acme-co", self.application.public_id])
        )

        self.assertContains(response, "The candidate withdrew")
        self.assertNotContains(response, 'name="action" value="move"')

    def test_a_live_application_still_offers_it(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("employer:application_detail", args=["acme-co", self.application.public_id])
        )
        self.assertContains(response, 'name="action" value="move"')
