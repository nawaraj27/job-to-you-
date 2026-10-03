"""
Domain proof (SRS §19.2 "domain proof, then documents"; §5.2 work-email proof).
Loop Closure EMP-03: `CompanyDomain` had no service and no screen.
"""

from __future__ import annotations

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.services import identity
from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.common.errors import Conflict, InvalidState, NotAllowed
from apps.companies.models import CompanyDomain
from apps.companies.permissions import Role
from apps.companies.services import domains
from apps.companies.tests.factories import add_member, company_with_owner


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DomainProofTests(TestCase):
    def setUp(self):
        self.owner = create_user(email="owner@gmail.com")
        self.company, self.membership = company_with_owner(
            self.owner, slug="himal-traders", website="https://www.himaltraders.com.np/"
        )
        self.url = reverse("employer:verification", args=["himal-traders"])

    def test_the_domain_comes_from_the_website_and_free_mail_does_not_count(self):
        self.assertEqual(domains.website_domain(self.company), "himaltraders.com.np")
        self.company.website = "https://gmail.com"
        self.assertEqual(domains.website_domain(self.company), "")

    def test_an_address_outside_the_domain_is_refused(self):
        with self.assertRaisesMessage(InvalidState, "@himaltraders.com.np"):
            domains.add_work_email(
                actor=self.owner,
                membership=self.membership,
                company=self.company,
                email="me@gmail.com",
            )

    def test_the_full_journey_through_the_screen(self):
        self.client.force_login(self.owner)
        page = self.client.get(self.url)
        self.assertContains(page, "Work email at himaltraders.com.np")

        self.client.post(self.url, {"action": "domain_email", "email": "ops@himaltraders.com.np"})
        self.assertEqual(len(mail.outbox), 1)
        link = next(
            line for line in mail.outbox[0].body.splitlines() if "/accounts/verify/" in line
        )
        token = link.rsplit("/accounts/verify/", 1)[1].strip("/")

        # Not proven until the address is confirmed.
        response = self.client.post(self.url, {"action": "prove_domain"}, follow=True)
        self.assertContains(response, "Verify an address ending in @himaltraders.com.np first")

        identity.verify_email(token=token)
        page = self.client.get(self.url)
        self.assertContains(page, "Confirm himaltraders.com.np for")

        response = self.client.post(self.url, {"action": "prove_domain"}, follow=True)
        self.assertContains(response, "himaltraders.com.np is proven")
        record = CompanyDomain.objects.get(company=self.company)
        self.assertIsNotNone(record.verified_at)
        self.assertTrue(AuditLog.objects.filter(verb="company.domain_proven").exists())

    def test_another_company_cannot_prove_the_same_domain(self):
        CompanyDomain.objects.create(
            company=company_with_owner(create_user(), website="https://himaltraders.com.np")[0],
            domain="himaltraders.com.np",
            verified_at="2026-09-01T00:00:00Z",
        )
        address, token = domains.add_work_email(
            actor=self.owner,
            membership=self.membership,
            company=self.company,
            email="ops@himaltraders.com.np",
        )
        identity.verify_email(token=token)
        with self.assertRaises(Conflict):
            domains.prove_domain(actor=self.owner, membership=self.membership, company=self.company)

    def test_a_recruiter_cannot_start_or_record_proof(self):
        recruiter = create_user()
        membership = add_member(self.company, recruiter, Role.RECRUITER)
        with self.assertRaises(NotAllowed):
            domains.add_work_email(
                actor=recruiter,
                membership=membership,
                company=self.company,
                email="r@himaltraders.com.np",
            )
        with self.assertRaises(NotAllowed):
            domains.prove_domain(actor=recruiter, membership=membership, company=self.company)

    def test_an_address_owned_by_another_account_is_refused(self):
        other = create_user()
        from apps.accounts.models import EmailAddress

        EmailAddress.objects.create(user=other, email="ops@himaltraders.com.np")
        with self.assertRaises(Conflict):
            domains.add_work_email(
                actor=self.owner,
                membership=self.membership,
                company=self.company,
                email="ops@himaltraders.com.np",
            )
