"""
Legal documents and the consent that points at them (SRS §5.14, §15.5, A33).

Three properties:

  * **A version is reproducible.** `Consent.policy_version` records a string,
    and that string has to resolve to the text somebody actually read. So a
    published version is never edited — a change is a new row.
  * **The page dates itself from the document.** It previously rendered
    `{% now %}`, claiming to have been updated today whatever the text said.
  * **Registration records consent.** `Consent` exists to answer "what was
    agreed, when, under which text"; until P19 nothing wrote to it, so it
    answered with silence.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Consent
from apps.accounts.services import identity
from apps.common.errors import Conflict
from apps.core import legal, legal_seeds
from apps.core.models import LegalDocument


class LegalDocumentTests(TestCase):
    def setUp(self):
        call_command("seed_legal", quiet_summary=True)

    def test_the_seed_publishes_both_documents(self):
        self.assertEqual(LegalDocument.objects.count(), 2)

    def test_the_seed_is_idempotent(self):
        call_command("seed_legal", quiet_summary=True)
        self.assertEqual(LegalDocument.objects.count(), 2)

    def test_current_returns_the_version_in_force(self):
        document = legal.current("terms")
        self.assertEqual(document.version, legal_seeds.DRAFT_VERSION)

    def test_a_future_version_is_not_in_force_yet(self):
        legal.publish(
            actor=None,
            slug="terms",
            version="1.0",
            title="Terms of use",
            body="<p>Reviewed.</p>",
            effective_from=timezone.localdate() + dt.timedelta(days=7),
            is_draft=False,
        )
        self.assertEqual(legal.current("terms").version, legal_seeds.DRAFT_VERSION)

    def test_it_takes_over_on_its_effective_date(self):
        legal.publish(
            actor=None,
            slug="terms",
            version="1.0",
            title="Terms of use",
            body="<p>Reviewed.</p>",
            effective_from=timezone.localdate(),
            is_draft=False,
        )
        self.assertEqual(legal.current("terms").version, "1.0")

    def test_republishing_a_version_is_refused(self):
        # Editing a published version rewrites the text behind somebody's
        # consent, and `policy_version` would point at prose that no longer
        # exists.
        with self.assertRaises(Conflict):
            legal.publish(
                actor=None,
                slug="terms",
                version=legal_seeds.DRAFT_VERSION,
                title="Terms",
                body="<p>Different.</p>",
            )

    def test_publishing_is_audited(self):
        from apps.audit.models import AuditLog

        self.assertTrue(AuditLog.objects.filter(verb="legal.document_published").exists())

    def test_version_of_is_empty_when_nothing_is_published(self):
        LegalDocument.objects.all().delete()
        self.assertEqual(legal.version_of("terms"), "")


class LegalPageTests(TestCase):
    def setUp(self):
        call_command("seed_legal", quiet_summary=True)

    def test_the_terms_page_renders_the_stored_body(self):
        response = self.client.get(reverse("core:terms"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acceptable use")

    def test_the_page_shows_the_version_and_its_real_date(self):
        document = legal.current("terms")
        response = self.client.get(reverse("core:terms"))

        self.assertContains(response, document.version)
        self.assertContains(response, document.effective_from.strftime("%d %B %Y").lstrip("0"))

    def test_a_draft_says_so(self):
        response = self.client.get(reverse("core:privacy"))
        self.assertContains(response, "not yet been reviewed by a lawyer")

    def test_a_reviewed_version_drops_the_banner(self):
        legal.publish(
            actor=None,
            slug="privacy",
            version="1.0",
            title="Privacy policy",
            body="<p>Reviewed by counsel.</p>",
            is_draft=False,
        )
        response = self.client.get(reverse("core:privacy"))
        self.assertNotContains(response, "not yet been reviewed by a lawyer")

    def test_an_unseeded_environment_says_so_rather_than_crashing(self):
        LegalDocument.objects.all().delete()
        response = self.client.get(reverse("core:terms"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "has not been published yet")

    def test_the_privacy_page_states_what_erasure_cannot_undo(self):
        # §19.4 requires both limits to be stated rather than implied, and the
        # policy is where somebody reads them before deciding.
        response = self.client.get(reverse("core:privacy"))
        self.assertContains(response, "unsent")

    def test_the_privacy_page_states_the_retention_schedule(self):
        from apps.common import retention

        response = self.client.get(reverse("core:privacy"))
        self.assertContains(response, "twelve months")
        self.assertEqual(retention.INTERVIEW_TRANSCRIPT_DAYS, 365)


class SignupConsentTests(TestCase):
    def setUp(self):
        call_command("seed_legal", quiet_summary=True)

    def test_registering_records_both_consents(self):
        user = identity.register(email="priya@example.com", password="a-long-password-1")

        purposes = set(
            Consent.objects.filter(user=user, granted=True).values_list("purpose", flat=True)
        )
        self.assertEqual(purposes, {Consent.Purpose.TERMS, Consent.Purpose.PRIVACY})

    def test_the_recorded_version_is_the_one_in_force(self):
        user = identity.register(email="ram@example.com", password="a-long-password-1")

        record = Consent.objects.get(user=user, purpose=Consent.Purpose.TERMS)
        self.assertEqual(record.policy_version, legal.version_of("terms"))

    def test_the_evidence_says_where_it_came_from(self):
        user = identity.register(email="sita@example.com", password="a-long-password-1")

        record = Consent.objects.get(user=user, purpose=Consent.Purpose.PRIVACY)
        self.assertEqual(record.evidence["source"], "registration")

    def test_registration_is_not_blocked_when_nothing_is_published(self):
        # An operator's missing seed must not stop somebody creating an account.
        LegalDocument.objects.all().delete()
        user = identity.register(email="hari@example.com", password="a-long-password-1")

        self.assertEqual(
            Consent.objects.get(user=user, purpose=Consent.Purpose.TERMS).policy_version, ""
        )

    def test_outstanding_is_empty_for_somebody_who_just_signed_up(self):
        user = identity.register(email="gita@example.com", password="a-long-password-1")
        self.assertEqual(legal.outstanding_for(user), [])

    def test_a_new_version_becomes_outstanding(self):
        user = identity.register(email="bikash@example.com", password="a-long-password-1")
        legal.publish(
            actor=None,
            slug="terms",
            version="1.0",
            title="Terms of use",
            body="<p>Reviewed.</p>",
            is_draft=False,
        )

        outstanding = legal.outstanding_for(user)
        self.assertEqual([document.slug for document in outstanding], ["terms"])
