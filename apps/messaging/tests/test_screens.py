"""
Messaging screens, proved through real HTTP.

Two properties that only a request can demonstrate: another party's thread is
unreachable from either portal, and there is no route that opens a conversation
without an application behind it.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.models import BlockedCompany
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.messaging.models import Conversation, ConversationState, Message, SenderKind
from apps.messaging.services import conversations as service


class EmployerScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        self.application = submit(job=self.job, profile=self.profile)
        self.client.force_login(self.owner)

    def test_the_inbox_opens_empty(self):
        response = self.client.get(reverse("employer:messages", args=["acme-co"]))
        self.assertEqual(response.status_code, 200)

    def test_a_thread_starts_from_an_application(self):
        response = self.client.post(
            reverse("employer:application_message", args=["acme-co", self.application.public_id]),
            {"body": "Are you free Thursday?"},
        )

        conversation = Conversation.objects.get()
        self.assertEqual(conversation.application, self.application)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(response.status_code, 302)

    def test_an_application_from_another_company_cannot_start_one(self):
        other, _ = company_with_owner(create_user(email="them@example.test"), slug="them-co")
        theirs = submit(job=publish(create_job(other)), profile=create_profile())

        response = self.client.post(
            reverse("employer:application_message", args=["acme-co", theirs.public_id]),
            {"body": "Hello"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_another_companys_thread_is_a_404(self):
        other, other_membership = company_with_owner(
            create_user(email="them@example.test"), slug="them-co"
        )
        their_candidate = create_profile()
        submit(job=publish(create_job(other)), profile=their_candidate)
        theirs = service.open_conversation(
            company=other,
            candidate=their_candidate,
            actor=other_membership.user,
            membership=other_membership,
        )

        response = self.client.get(
            reverse("employer:messages", args=["acme-co"]), {"thread": theirs.public_id}
        )
        self.assertEqual(response.status_code, 404)

    def test_opening_a_thread_marks_it_read(self):
        conversation = service.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
        )
        service.send(
            conversation=conversation,
            actor=self.profile.user,
            sender_kind=SenderKind.CANDIDATE,
            body="Yes, Thursday works",
        )
        self.assertEqual(conversation.unread_for_company(), 1)

        self.client.get(
            reverse("employer:messages", args=["acme-co"]), {"thread": conversation.public_id}
        )

        conversation.refresh_from_db()
        self.assertEqual(conversation.unread_for_company(), 0)


class CandidateScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user(email="owner@example.test")
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.user = create_user()
        self.profile = create_profile(user=self.user)
        submit(job=self.job, profile=self.profile)
        self.conversation = service.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
        )
        service.send(
            conversation=self.conversation,
            actor=self.owner,
            sender_kind=SenderKind.COMPANY,
            body="Are you free Thursday?",
        )
        self.client.force_login(self.user)

    def test_the_inbox_lists_the_thread(self):
        response = self.client.get(reverse("messages:inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.company.name)

    def test_the_thread_shows_the_message(self):
        response = self.client.get(
            reverse("messages:inbox"), {"thread": self.conversation.public_id}
        )
        self.assertContains(response, "Are you free Thursday?")

    def test_another_persons_thread_is_a_404(self):
        other_candidate = create_profile()
        submit(job=self.job, profile=other_candidate)
        theirs = service.open_conversation(
            company=self.company,
            candidate=other_candidate,
            actor=self.owner,
            membership=self.membership,
        )

        response = self.client.get(reverse("messages:inbox"), {"thread": theirs.public_id})
        self.assertEqual(response.status_code, 404)

    def test_replying_works(self):
        self.client.post(
            reverse("messages:inbox"),
            {"thread": self.conversation.public_id, "action": "send", "body": "Yes"},
        )
        self.assertTrue(Message.objects.filter(sender_kind=SenderKind.CANDIDATE).exists())

    def test_blocking_from_the_screen_has_all_three_effects(self):
        self.client.post(
            reverse("messages:inbox"),
            {"thread": self.conversation.public_id, "action": "block", "reason": "Spam"},
        )

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.state, ConversationState.BLOCKED)
        self.assertTrue(
            BlockedCompany.objects.filter(profile=self.profile, company=self.company).exists()
        )

        from apps.applications.services import visibility

        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_muting_from_the_screen_works(self):
        self.client.post(
            reverse("messages:inbox"),
            {"thread": self.conversation.public_id, "action": "mute"},
        )

        self.conversation.refresh_from_db()
        self.assertIsNotNone(self.conversation.candidate_muted_at)

    def test_reporting_from_the_screen_works(self):
        self.client.post(
            reverse("messages:inbox"),
            {"thread": self.conversation.public_id, "action": "report", "reason": "spam"},
        )
        self.assertEqual(self.conversation.reports.count(), 1)

    def test_an_unknown_action_is_a_404(self):
        response = self.client.post(
            reverse("messages:inbox"),
            {"thread": self.conversation.public_id, "action": "explode"},
        )
        self.assertEqual(response.status_code, 404)

    def test_signing_out_sends_you_to_sign_in(self):
        self.client.logout()
        response = self.client.get(reverse("messages:inbox"))
        self.assertEqual(response.status_code, 302)
