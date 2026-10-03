"""
Employer onboarding screens, driven through HTTP (section loop, 2026-09-14).

What the running product did before this loop:

  * the verification screen had a "Submit for review" button and no way to
    attach a document, so every submission was refused;
  * the team screen listed pending invitations with no way to withdraw one and
    members with no way to change a role;
  * a person invited by email who had no account signed up from the sign-in
    page and landed on a dashboard: the invitation link was gone;
  * a website typed as "not a url" was saved.
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.companies.models import CompanyInvite, CompanyMembership, CompanyVerification
from apps.companies.permissions import Role
from apps.companies.services import company as company_service
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member, company_with_owner
from apps.files.scanning import reset_scanner_cache
from apps.files.tests import factories as file_factories


class VerificationScreenTests(TestCase):
    def setUp(self):
        reset_scanner_cache()
        self.addCleanup(reset_scanner_cache)
        self.owner = create_user()
        self.company = company_service.create_company(actor=self.owner, name="Screen Co")
        self.url = reverse("employer:verification", args=[self.company.slug])
        self.client.force_login(self.owner)

    def _upload(self, content=None):
        document = (
            file_factories.pdf_upload("registration.pdf")
            if content is None
            else file_factories.upload(content, "registration.pdf", "application/pdf")
        )
        return self.client.post(
            self.url, {"action": "upload", "kind": "registration", "document": document}
        )

    def test_the_screen_offers_an_upload_and_holds_submit_until_a_document_exists(self):
        response = self.client.get(self.url)

        self.assertContains(response, 'type="file"')
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, "Company registration certificate")
        self.assertContains(response, 'disabled aria-describedby="submit-hint"')

    def test_uploading_through_the_form_attaches_the_document(self):
        response = self._upload()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.company.verification.documents.count(), 1)
        self.assertContains(self.client.get(self.url), "registration.pdf")

    def test_a_company_can_upload_then_submit_and_the_documents_freeze(self):
        self._upload()
        self.client.post(self.url, {"action": "submit"})

        self.company.refresh_from_db()
        self.assertEqual(self.company.verification_state, "pending")
        page = self.client.get(self.url)
        self.assertNotContains(page, 'type="file"')
        self.assertContains(page, "Documents cannot change while the review is in progress")

    def test_bytes_that_are_not_a_document_are_refused_with_a_message(self):
        response = self._upload(content=b"MZ\x90\x00 executable")

        self.assertEqual(self.company.verification.documents.count(), 0)
        self.assertContains(self.client.get(response["Location"]), "not accepted")

    def test_a_recruiter_sees_no_upload_and_cannot_post_one(self):
        recruiter = create_user()
        add_member(self.company, recruiter, Role.RECRUITER)
        self.client.force_login(recruiter)

        page = self.client.get(self.url)
        self.assertNotContains(page, 'type="file"')
        self.assertContains(page, "Only an owner, an admin")
        self._upload()
        self.assertEqual(self.company.verification.documents.count(), 0)

    def test_a_rejected_company_sees_the_reason_and_how_to_contest_it(self):
        CompanyVerification.objects.filter(company=self.company).update(
            state="rejected", reason="The registration number does not match."
        )

        page = self.client.get(self.url)
        self.assertContains(page, "The registration number does not match.")
        self.assertContains(page, reverse("core:contact"))
        self.assertNotContains(page, 'type="file"')

    def test_an_unknown_action_is_404(self):
        self.assertEqual(self.client.post(self.url, {"action": "approve"}).status_code, 404)


class TeamScreenTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner, slug="team-co")
        self.recruiter = add_member(self.company, create_user(), Role.RECRUITER)
        self.invite, _ = membership_service.invite(
            actor=self.owner,
            actor_membership=self.owner_membership,
            company=self.company,
            email="pending@example.com",
            role=Role.RECRUITER,
        )
        self.url = reverse("employer:team", args=["team-co"])
        self.client.force_login(self.owner)

    def test_an_invitation_can_be_withdrawn_from_the_page(self):
        self.assertContains(self.client.get(self.url), 'value="revoke"')

        self.client.post(self.url, {"action": "revoke", "invite_id": self.invite.pk})

        self.invite.refresh_from_db()
        self.assertIsNotNone(self.invite.revoked_at)
        self.assertEqual(list(self.client.get(self.url).context["invites"]), [])

    def test_a_role_can_be_changed_from_the_page(self):
        self.client.post(
            self.url, {"action": "role", "membership_id": self.recruiter.pk, "role": Role.VIEWER}
        )
        self.recruiter.refresh_from_db()
        self.assertEqual(self.recruiter.role, Role.VIEWER)

    def test_an_unknown_role_changes_nothing_and_says_why(self):
        response = self.client.post(
            self.url,
            {"action": "role", "membership_id": self.recruiter.pk, "role": "superuser"},
            follow=True,
        )
        self.recruiter.refresh_from_db()
        self.assertEqual(self.recruiter.role, Role.RECRUITER)
        self.assertContains(response, "Choose a role")

    def test_a_blank_invitation_email_is_refused_with_a_message(self):
        response = self.client.post(
            self.url, {"action": "invite", "email": "", "role": Role.RECRUITER}, follow=True
        )
        self.assertContains(response, "Enter the email address")
        self.assertFalse(CompanyInvite.objects.filter(company=self.company, email="").exists())

    def test_an_admin_is_not_offered_the_owner_role(self):
        admin = create_user()
        add_member(self.company, admin, Role.ADMIN)
        self.client.force_login(admin)

        page = self.client.get(self.url)
        self.assertNotContains(page, '<option value="owner"')
        self.assertNotContains(page, "Transfer ownership to")

    def test_ownership_transfer_is_behind_a_confirmation(self):
        page = self.client.get(self.url)
        self.assertContains(page, "<details")
        self.assertContains(page, "You become a company admin")

    def test_the_ownership_buttons_wrap_a_long_name_on_a_phone(self):
        # Found by the 320 px overflow scan (2026-09-18): "Make Sweep Hiring
        # manager the owner" was 324 px wide because a button does not wrap.
        self.recruiter.user.full_name = "Someone With A Very Long Full Name Indeed"
        self.recruiter.user.save(update_fields=["full_name"])

        page = self.client.get(self.url).content.decode()

        summary = page.split('<summary class="', 1)[1].split('"', 1)[0]
        nominate = page.split('value="nominate"', 1)[0].rsplit('<button class="', 1)[1]
        for classes in (summary, nominate):
            self.assertIn("whitespace-normal", classes)
            self.assertIn("max-w-full", classes)


class CreateCompanyScreenTests(TestCase):
    def test_an_invalid_website_keeps_what_was_typed_and_creates_nothing(self):
        user = create_user()
        self.client.force_login(user)

        response = self.client.post(
            reverse("employer:create"), {"name": "Typed Name Pvt Ltd", "website": "not a url"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'value="Typed Name Pvt Ltd"', status_code=400)
        self.assertFalse(CompanyMembership.objects.filter(user=user).exists())


class InviteSignupRoundTripTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        owner = create_user()
        self.company, owner_membership = company_with_owner(owner, slug="invite-co")
        _, token = membership_service.invite(
            actor=owner,
            actor_membership=owner_membership,
            company=self.company,
            email="fresh.person@example.com",
            role=Role.RECRUITER,
        )
        self.accept = reverse("employer:accept_invite", args=[token])

    def _signup(self, next_url):
        return self.client.post(
            reverse("accounts:signup"),
            {
                "signup_intent": "candidate",
                "full_name": "Fresh Person",
                "email": "fresh.person@example.com",
                "password1": "Fresh-person-2026",
                "password2": "Fresh-person-2026",
                "next": next_url,
            },
        )

    def test_the_sign_in_page_carries_the_invitation_into_sign_up(self):
        login = self.client.get(self.accept, follow=True)
        self.assertContains(login, "signup/?next=" + self.accept)

        signup = self.client.get(reverse("accounts:signup"), {"next": self.accept})
        self.assertContains(signup, f'name="next" value="{self.accept}"')

    def test_signing_up_returns_to_the_invitation_and_it_can_be_accepted(self):
        # Decision D-12: sign-up signs nobody in. The invitation rides through
        # the "check your email" page, the confirmation link and sign-in.
        import re
        from urllib.parse import quote

        from django.core import mail

        response = self._signup(self.accept)
        self.assertIn(quote(self.accept, safe=""), response["Location"])
        link = re.search(r"http://testserver(/accounts/verify/\S+)", mail.outbox[-1].body)
        confirmed = self.client.get(link.group(1))
        self.assertIn(quote(self.accept, safe=""), confirmed["Location"])
        signed_in = self.client.post(
            confirmed["Location"],
            {"username": "fresh.person@example.com", "password": "Fresh-person-2026"},
        )
        self.assertRedirects(signed_in, self.accept, fetch_redirect_response=False)

        self.client.post(self.accept)
        self.assertTrue(
            CompanyMembership.objects.filter(
                company=self.company, user__email="fresh.person@example.com"
            ).exists()
        )

    def test_an_off_site_next_is_ignored(self):
        response = self._signup("https://evil.example/steal")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response["Location"].startswith("https://evil.example"))
