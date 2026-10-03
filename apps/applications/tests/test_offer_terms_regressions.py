"""
FR-6.8 (Should): "Offers with salary, start date, expiry and an attached
document" (Loop Closure JOB-01).

The service accepted an expiry but the employer form never offered one, so
every offer silently got the 14-day default; the message the candidate reads
could not be written from the screen; and there was no way to attach an offer
letter at all, although `Offer.attachment` exists.
"""

from __future__ import annotations

import datetime as dt

from django.urls import reverse
from django.utils import timezone

from apps.applications.models import Offer
from apps.applications.services import offers as offer_service
from apps.applications.tests.test_offer_regressions import OfferFixture
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.files import policies as file_policies
from apps.files.tests import factories as file_factories


def _clean(offer):
    offer.attachment.scan_status = "clean"
    offer.attachment.save(update_fields=["scan_status"])


class OfferTermsScreenTests(OfferFixture):
    def post_draft(self, **extra):
        self.client.force_login(self.owner)
        data = {"action": "draft", "salary": "150000", "currency": "NPR", "period": "month"}
        data.update(extra)
        return self.client.post(
            reverse("employer:application_offer", args=["acme-co", self.application.public_id]),
            data,
            follow=True,
        )

    def test_the_form_offers_expiry_message_and_letter(self):
        self.client.force_login(self.owner)
        page = self.client.get(
            reverse("employer:application_detail", args=["acme-co", self.application.public_id])
        )
        for name in (
            'name="expires_on"',
            'name="message"',
            'name="document"',
            "multipart/form-data",
        ):
            with self.subTest(field=name):
                self.assertContains(page, name)

    def test_a_chosen_expiry_and_message_are_saved(self):
        closes = timezone.localdate() + dt.timedelta(days=5)
        self.post_draft(expires_on=closes.isoformat(), message="We would love you to join.")

        offer = Offer.objects.get(application=self.application)
        self.assertEqual(timezone.localtime(offer.expires_at).date(), closes)
        self.assertEqual(offer.message, "We would love you to join.")

    def test_an_expiry_already_gone_is_refused_with_a_reason(self):
        response = self.post_draft(
            expires_on=(timezone.localdate() - dt.timedelta(days=1)).isoformat()
        )
        self.assertContains(response, "must stay open")
        self.assertFalse(Offer.objects.filter(application=self.application).exists())

    def test_the_default_is_still_fourteen_days(self):
        self.post_draft()
        offer = Offer.objects.get(application=self.application)
        self.assertEqual(
            (timezone.localtime(offer.expires_at).date() - timezone.localdate()).days,
            offer_service.DEFAULT_VALID_DAYS,
        )

    def test_a_letter_is_attached_and_readable_by_the_team_only_until_sent(self):
        self.post_draft(document=file_factories.pdf_upload())
        offer = Offer.objects.get(application=self.application)
        _clean(offer)

        self.assertTrue(file_policies.can_access(self.owner, offer.attachment))
        self.assertFalse(file_policies.can_access(self.candidate.user, offer.attachment))
        self.assertFalse(file_policies.can_access(create_profile().user, offer.attachment))

        offer_service.send(offer=offer, actor=self.owner, membership=self.membership)

        self.assertTrue(file_policies.can_access(self.candidate.user, offer.attachment))
        self.assertFalse(file_policies.can_access(create_profile().user, offer.attachment))

    def test_the_candidate_screen_links_the_letter_once_sent_and_it_opens(self):
        self.post_draft(document=file_factories.pdf_upload())
        offer = Offer.objects.get(application=self.application)
        _clean(offer)
        detail = reverse("applications:detail", args=[self.application.public_id])

        self.client.force_login(self.candidate.user)
        self.assertNotContains(self.client.get(detail), "Read the offer letter")

        offer_service.send(offer=offer, actor=self.owner, membership=self.membership)
        page = self.client.get(detail)
        self.assertContains(page, "Read the offer letter")

        link = page.context["offer_document_url"]
        response = self.client.get(link)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content)[:4], b"%PDF")

    def test_a_past_expiry_is_refused_by_the_service_too(self):
        with self.assertRaisesMessage(InvalidState, "must stay open"):
            self.draft(expires_at=timezone.now() - dt.timedelta(minutes=1))
