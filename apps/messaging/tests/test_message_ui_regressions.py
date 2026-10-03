"""
Messaging attachments, edit and delete, through both inboxes (Loop Closure
MSG-01, MSG-02, MSG-05).

  * FR-7.5: attachments existed in the service but neither reply form could
    send one;
  * editing (15-minute window) and deleting existed in the service with no
    control on either screen;
  * both inboxes linked attachments to `files:access` with no token, so every
    attachment link was a 404.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.files.models import AttachmentAccess
from apps.files.tests import factories as file_factories
from apps.jobs.tests.factories import create_job, publish
from apps.messaging.models import Message, SenderKind
from apps.messaging.services import conversations as service


class MessageUiFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company))
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
        self.employer_url = reverse("employer:messages", args=["acme-co"])
        self.candidate_url = reverse("messages:inbox")

    def employer_post(self, **data):
        self.client.force_login(self.owner)
        return self.client.post(
            self.employer_url, {"thread": self.thread.public_id, **data}, follow=True
        )

    def candidate_post(self, **data):
        self.client.force_login(self.profile.user)
        return self.client.post(
            self.candidate_url, {"thread": self.thread.public_id, **data}, follow=True
        )


class AttachmentTests(MessageUiFixture):
    def test_both_reply_forms_accept_files(self):
        self.client.force_login(self.owner)
        page = self.client.get(self.employer_url, {"thread": self.thread.public_id})
        self.assertContains(page, 'name="attachments"')
        self.assertContains(page, "multipart/form-data")

        self.client.force_login(self.profile.user)
        page = self.client.get(self.candidate_url, {"thread": self.thread.public_id})
        self.assertContains(page, 'name="attachments"')

    def test_a_file_sent_by_the_employer_opens_for_the_candidate_by_a_tokened_link(self):
        self.employer_post(
            action="send", body="Our offer pack.", attachments=[file_factories.pdf_upload()]
        )
        message = Message.objects.get(body="Our offer pack.")
        attachment = message.attachments.get()
        attachment.scan_status = "clean"
        attachment.save(update_fields=["scan_status"])

        self.client.force_login(self.profile.user)
        page = self.client.get(self.candidate_url, {"thread": self.thread.public_id})
        row = next(r for r in page.context["rows"] if r.message.pk == message.pk)
        link = row.attachments[0].url
        self.assertIn("?t=", link)
        self.assertContains(page, link)

        response = self.client.get(link)
        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)
        self.assertTrue(AttachmentAccess.objects.filter(attachment=attachment).exists())

    def test_a_file_still_being_scanned_is_named_without_a_link(self):
        self.employer_post(
            action="send", body="Pending file.", attachments=[file_factories.pdf_upload()]
        )

        self.client.force_login(self.profile.user)
        page = self.client.get(self.candidate_url, {"thread": self.thread.public_id})
        self.assertContains(page, "Being checked for viruses")

    def test_a_wrong_file_type_is_refused_and_no_message_is_written(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bogus = SimpleUploadedFile(
            "run.exe", b"MZ\x90\x00not a document", "application/octet-stream"
        )
        response = self.candidate_post(action="send", body="See attached", attachments=[bogus])

        self.assertFalse(Message.objects.filter(body="See attached").exists())
        self.assertContains(response, "not accepted")

    def test_more_than_the_limit_is_refused(self):
        uploads = [file_factories.pdf_upload() for _ in range(service.MAX_ATTACHMENTS + 1)]
        response = self.candidate_post(action="send", body="Too many", attachments=uploads)

        self.assertFalse(Message.objects.filter(body="Too many").exists())
        self.assertContains(response, f"Up to {service.MAX_ATTACHMENTS} attachments")


class EditDeleteScreenTests(MessageUiFixture):
    def setUp(self):
        super().setUp()
        self.mine = service.send(
            conversation=self.thread,
            actor=self.profile.user,
            sender_kind=SenderKind.CANDIDATE,
            body="Thnaks for the update",
        )

    def test_own_recent_message_offers_edit_and_delete_others_do_not(self):
        theirs = service.send(
            conversation=self.thread, actor=self.owner, sender_kind=SenderKind.COMPANY, body="Hi"
        )
        self.client.force_login(self.profile.user)
        page = self.client.get(self.candidate_url, {"thread": self.thread.public_id})
        rows = {row.message.pk: row for row in page.context["rows"]}

        self.assertTrue(rows[self.mine.pk].can_edit)
        self.assertTrue(rows[self.mine.pk].can_delete)
        self.assertFalse(rows[theirs.pk].can_edit)
        self.assertFalse(rows[theirs.pk].can_delete)
        self.assertContains(page, "Edit or delete")

    def test_editing_through_the_screen(self):
        response = self.candidate_post(
            action="edit", message_id=self.mine.pk, body="Thanks for the update"
        )

        self.mine.refresh_from_db()
        self.assertEqual(self.mine.body, "Thanks for the update")
        self.assertIsNotNone(self.mine.edited_at)
        self.assertContains(response, "Message edited.")

    def test_editing_after_the_window_is_refused_and_not_offered(self):
        Message.objects.filter(pk=self.mine.pk).update(
            created_at=timezone.now() - dt.timedelta(minutes=service.EDIT_WINDOW_MINUTES + 1)
        )
        response = self.candidate_post(action="edit", message_id=self.mine.pk, body="Late edit")

        self.mine.refresh_from_db()
        self.assertEqual(self.mine.body, "Thnaks for the update")
        self.assertContains(response, "can be edited for")
        row = next(r for r in response.context["rows"] if r.message.pk == self.mine.pk)
        self.assertFalse(row.can_edit)
        self.assertTrue(row.can_delete)

    def test_deleting_through_the_screen_leaves_a_tombstone(self):
        response = self.candidate_post(action="delete", message_id=self.mine.pk)

        self.mine.refresh_from_db()
        self.assertIsNotNone(self.mine.deleted_at)
        self.assertContains(response, "Message deleted.")

    def test_the_employer_cannot_edit_or_delete_the_candidate_s_message(self):
        for action in ("edit", "delete"):
            with self.subTest(action=action):
                response = self.employer_post(
                    action=action, message_id=self.mine.pk, body="Changed"
                )
                self.assertContains(response, "your own messages")
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.body, "Thnaks for the update")
        self.assertIsNone(self.mine.deleted_at)

    def test_a_message_from_another_thread_is_not_found(self):
        other_profile = create_profile()
        other_application = submit(job=self.job, profile=other_profile)
        other_thread = service.open_conversation(
            company=self.company,
            candidate=other_profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
            application=other_application,
        )
        foreign = service.send(
            conversation=other_thread, actor=self.owner, sender_kind=SenderKind.COMPANY, body="x"
        )

        for value in (foreign.pk, "not-a-number"):
            with self.subTest(message_id=value):
                response = self.employer_post(action="delete", message_id=value)
                self.assertContains(response, "not in this conversation")
        foreign.refresh_from_db()
        self.assertIsNone(foreign.deleted_at)
