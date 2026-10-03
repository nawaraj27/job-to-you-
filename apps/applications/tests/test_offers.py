"""
Offers (FR-6.8).

A real state machine, and the tests are mostly about moves it refuses: an
accepted offer that could be edited or re-sent would make "what did we agree"
unanswerable, which is the one question an offer exists to answer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.applications.models import Offer, OfferState
from apps.applications.services import offers as offer_service
from apps.applications.tests.factories import seed_templates, submit
from apps.audit.models import AuditLog
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotAllowed
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import OutboxEvent


class OfferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        self.application = submit(job=self.job, profile=self.profile)

    def draft(self, **kwargs):
        kwargs.setdefault("salary", "1200000")
        kwargs.setdefault("currency", "NPR")
        return offer_service.draft_offer(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            **kwargs,
        )

    def sent(self):
        offer = self.draft()
        return offer_service.send(offer=offer, actor=self.owner, membership=self.membership)

    def test_a_draft_is_not_sent(self):
        offer = self.draft()
        self.assertEqual(offer.state, OfferState.DRAFT)
        self.assertIsNone(offer.sent_at)
        # Nothing on the candidate's timeline yet.
        self.assertFalse(self.application.timeline.filter(type="offer.sent").exists())

    def test_a_draft_gets_a_default_expiry(self):
        offer = self.draft()
        self.assertIsNotNone(offer.expires_at)

    def test_the_typed_salary_is_kept_and_a_comparable_one_derived(self):
        offer = self.draft(salary="1,200,000", currency="NPR", period="year")
        self.assertEqual(offer.salary, Decimal("1200000.00"))
        self.assertEqual(offer.currency, "NPR")

    def test_a_salary_with_a_symbol_is_refused(self):
        with self.assertRaises(InvalidState):
            self.draft(salary="Rs 1200000")

    def test_a_negative_salary_is_refused(self):
        with self.assertRaises(InvalidState):
            self.draft(salary="-1")

    def test_two_live_offers_cannot_exist_at_once(self):
        self.draft()
        with self.assertRaises(InvalidState):
            self.draft()

    def test_sending_needs_the_offer_grant(self):
        recruiter = create_user(email="recruiter@example.test")
        membership = add_member(self.company, recruiter, Role.RECRUITER)
        offer = self.draft()

        with self.assertRaises(NotAllowed):
            offer_service.send(offer=offer, actor=recruiter, membership=membership)

        offer.refresh_from_db()
        self.assertEqual(offer.state, OfferState.DRAFT)

    def test_sending_puts_it_on_the_candidate_timeline(self):
        self.sent()

        entry = self.application.timeline.filter(type="offer.sent").get()
        self.assertTrue(entry.visible_to_candidate)
        self.assertTrue(OutboxEvent.objects.filter(type="offer.sent").exists())
        self.assertTrue(AuditLog.objects.filter(verb="offer.sent").exists())

    def test_an_offer_with_no_number_cannot_be_sent(self):
        offer = self.draft(salary=None)
        with self.assertRaises(InvalidState):
            offer_service.send(offer=offer, actor=self.owner, membership=self.membership)

    def test_an_offer_expiring_in_the_past_cannot_be_sent(self):
        # A draft whose date passed while it sat unsent. Drafting with a date
        # already gone is refused earlier (test_offer_terms_regressions).
        offer = self.draft(expires_at=timezone.now() + dt.timedelta(days=1))
        type(offer).objects.filter(pk=offer.pk).update(
            expires_at=timezone.now() - dt.timedelta(days=1)
        )
        offer.refresh_from_db()
        with self.assertRaises(InvalidState):
            offer_service.send(offer=offer, actor=self.owner, membership=self.membership)

    def test_the_candidate_can_accept(self):
        offer = self.sent()
        offer_service.respond(offer=offer, actor=self.profile.user, accept=True)

        offer.refresh_from_db()
        self.assertEqual(offer.state, OfferState.ACCEPTED)
        self.assertIsNotNone(offer.decided_at)

    def test_the_candidate_can_decline_with_a_reason(self):
        offer = self.sent()
        offer_service.respond(
            offer=offer, actor=self.profile.user, accept=False, reason="Taking another role"
        )

        offer.refresh_from_db()
        self.assertEqual(offer.state, OfferState.DECLINED)
        self.assertEqual(offer.decline_reason, "Taking another role")

    def test_nobody_else_can_answer_an_offer(self):
        offer = self.sent()
        with self.assertRaises(NotAllowed):
            offer_service.respond(offer=offer, actor=self.owner, accept=True)

    def test_a_draft_cannot_be_accepted(self):
        offer = self.draft()
        with self.assertRaises(InvalidState):
            offer_service.respond(offer=offer, actor=self.profile.user, accept=True)

    def test_an_accepted_offer_cannot_be_answered_again(self):
        offer = self.sent()
        offer_service.respond(offer=offer, actor=self.profile.user, accept=True)

        with self.assertRaises(InvalidState):
            offer_service.respond(offer=offer, actor=self.profile.user, accept=False)

    def test_an_accepted_offer_cannot_be_withdrawn(self):
        offer = self.sent()
        offer_service.respond(offer=offer, actor=self.profile.user, accept=True)

        with self.assertRaises(InvalidState):
            offer_service.withdraw(offer=offer, actor=self.owner, membership=self.membership)

    def test_withdrawing_frees_the_slot_for_a_revised_offer(self):
        offer = self.sent()
        offer_service.withdraw(
            offer=offer, actor=self.owner, membership=self.membership, reason="Revising"
        )

        revised = self.draft(salary="1400000")
        self.assertEqual(revised.state, OfferState.DRAFT)
        # The history survives; a revision is a new row, not an edit.
        self.assertEqual(Offer.objects.filter(application=self.application).count(), 2)

    def test_an_expired_offer_cannot_be_accepted(self):
        offer = self.sent()
        Offer.objects.filter(pk=offer.pk).update(expires_at=timezone.now() - dt.timedelta(hours=1))
        offer.refresh_from_db()

        with self.assertRaises(InvalidState):
            offer_service.respond(offer=offer, actor=self.profile.user, accept=True)

        offer.refresh_from_db()
        self.assertEqual(offer.state, OfferState.EXPIRED)

    def test_the_sweep_expires_overdue_offers(self):
        offer = self.sent()
        Offer.objects.filter(pk=offer.pk).update(expires_at=timezone.now() - dt.timedelta(hours=1))

        self.assertEqual(offer_service.expire_due(), 1)
        offer.refresh_from_db()
        self.assertEqual(offer.state, OfferState.EXPIRED)

    def test_the_sweep_is_idempotent(self):
        offer = self.sent()
        Offer.objects.filter(pk=offer.pk).update(expires_at=timezone.now() - dt.timedelta(hours=1))
        offer_service.expire_due()
        self.assertEqual(offer_service.expire_due(), 0)

    def test_expiry_tells_the_candidate(self):
        offer = self.sent()
        Offer.objects.filter(pk=offer.pk).update(expires_at=timezone.now() - dt.timedelta(hours=1))
        offer_service.expire_due()

        entry = self.application.timeline.filter(type="offer.expired").get()
        self.assertTrue(entry.visible_to_candidate)

    def test_an_offer_cannot_be_drafted_on_a_closed_application(self):
        from apps.applications.services import pipeline as pipeline_service

        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "declined"),
            reason="No",
        )
        with self.assertRaises(InvalidState):
            self.draft()
