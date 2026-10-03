"""
Messaging defects reproduced on the running product.

  * a malformed `?thread=` (a truncated link, a typo) was a 500 on both inboxes;
  * the new-message notification said "You have a new message about a role" —
    no sender, no role, and no link to the thread (FR-7.4);
  * a message over 8,000 characters was silently cut short;
  * both inboxes were wider than a phone (377 px at 375, 339 px at 320);
  * unread badges ran one COUNT query per thread.
"""

from __future__ import annotations

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.messaging import selectors
from apps.messaging.models import SenderKind
from apps.messaging.services import conversations as service
from apps.notifications.models import Notification
from apps.notifications.services import fanout, outbox


def deliver() -> None:
    for _ in range(10):
        if not outbox.drain(500):
            break
    for _ in range(10):
        if not fanout.run_due(limit=500).get("chunks"):
            break


class MessagingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company, title="Fraud Analyst"))
        self.profile = create_profile()
        self.application = submit(job=self.job, profile=self.profile)
        self.thread = service.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
            application=self.application,
        )

    def send(self, body="Are you free Thursday?", kind=SenderKind.COMPANY, actor=None):
        return service.send(
            conversation=self.thread,
            actor=actor or (self.owner if kind == SenderKind.COMPANY else self.profile.user),
            sender_kind=kind,
            body=body,
        )


class MalformedThreadIdTests(MessagingFixture):
    def test_the_candidate_inbox_says_not_found(self):
        self.client.force_login(self.profile.user)
        self.assertEqual(self.client.get("/messages/", {"thread": "not-a-uuid"}).status_code, 404)
        self.assertEqual(
            self.client.post("/messages/", {"thread": "not-a-uuid", "body": "x"}).status_code, 404
        )

    def test_the_employer_inbox_says_not_found(self):
        self.client.force_login(self.owner)
        url = reverse("employer:messages", args=["acme-co"])
        self.assertEqual(self.client.get(url, {"thread": "zzz"}).status_code, 404)
        self.assertEqual(self.client.post(url, {"thread": "zzz", "body": "x"}).status_code, 404)


class NotificationTests(MessagingFixture):
    def test_the_candidate_is_told_who_wrote_about_what_with_a_link(self):
        deliver()
        self.send()
        deliver()

        row = Notification.objects.filter(recipient=self.profile.user, kind="message.sent").get()
        self.assertIn(self.company.name, row.title)
        self.assertIn("Fraud Analyst", row.body)
        self.assertEqual(
            row.target_url, f"{reverse('messages:inbox')}?thread={self.thread.public_id}"
        )

    def test_the_team_link_opens_the_thread_in_the_employer_portal(self):
        self.send()
        deliver()
        self.send(body="Thursday works.", kind=SenderKind.CANDIDATE)
        deliver()

        row = Notification.objects.filter(recipient=self.owner, kind="message.sent").get()
        self.assertEqual(
            row.target_url,
            f"{reverse('employer:messages', args=['acme-co'])}?thread={self.thread.public_id}",
        )
        self.assertIn("Fraud Analyst", row.body)


class LengthLimitTests(MessagingFixture):
    def test_an_over_long_message_is_refused_not_cut(self):
        with self.assertRaisesMessage(InvalidState, "up to 8,000 characters"):
            self.send(body="x" * 9000)
        self.assertEqual(self.thread.messages.count(), 0)

    def test_the_reply_box_enforces_the_same_limit(self):
        self.send()
        self.client.force_login(self.profile.user)
        response = self.client.get("/messages/", {"thread": str(self.thread.public_id)})
        self.assertContains(response, 'maxlength="8000"')


class LayoutTests(MessagingFixture):
    def test_both_inbox_thread_lists_can_shrink_on_a_phone(self):
        self.send()
        self.client.force_login(self.profile.user)
        self.assertContains(self.client.get("/messages/"), '<aside class="min-w-0">')
        self.client.force_login(self.owner)
        self.assertContains(
            self.client.get(reverse("employer:messages", args=["acme-co"])),
            '<aside class="min-w-0">',
        )


class UnreadQueryTests(MessagingFixture):
    def test_unread_counts_do_not_grow_with_the_number_of_threads(self):
        def inbox_queries(threads: int) -> int:
            for index in range(threads):
                job = publish(create_job(self.company, title=f"Role {index}"))
                application = submit(job=job, profile=self.profile)
                thread = service.open_conversation(
                    company=self.company,
                    candidate=self.profile,
                    actor=self.owner,
                    membership=self.membership,
                    job=job,
                    application=application,
                )
                service.send(
                    conversation=thread, actor=self.owner, sender_kind=SenderKind.COMPANY, body="Hi"
                )
            self.client.force_login(self.profile.user)
            self.client.get("/messages/")
            with CaptureQueriesContext(connection) as captured:
                self.client.get("/messages/")
            return len(captured.captured_queries)

        few = inbox_queries(1)
        many = inbox_queries(5)
        self.assertLessEqual(many - few, 1, (few, many))

    def test_the_annotated_count_matches_the_row_count(self):
        self.send()
        self.send(body="Second")
        rows = list(selectors.conversations_for_candidate(self.profile))
        self.assertEqual(rows[0].unread_for_candidate(), 2)
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.unread_for_candidate(), 2)
