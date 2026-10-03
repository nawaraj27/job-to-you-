"""
The weekly digest and the audience registry (SRS §13.3, §13.5).

The digest is for people who want to know what happened without being
interrupted while it happens, and the tests here are mostly about when it
should stay quiet: one unread item is already visible on the bell, and a
digest containing it is noise.

The audience tests matter because an audience resolves to a **query** that is
re-run per chunk. A resolver that returns the wrong set is a notification sent
to the wrong person, and the failing direction — too many recipients — is the
one that leaks.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import (
    EmailMessage,
    EmailStatus,
    Notification,
    NotificationPreference,
    OutboxEvent,
    SuppressionReason,
)
from apps.notifications.services import audiences, channels, digests


def _notification(user, **overrides) -> Notification:
    fields = {
        "recipient": user,
        "kind": "application.submitted",
        "event_key": "application_updates",
        "title": "New application",
        "body": "Somebody applied.",
    }
    fields.update(overrides)
    return Notification.objects.create(**fields)


class DigestTests(TestCase):
    def setUp(self):
        self.user = create_user(email="reader@example.test")
        NotificationPreference.objects.create(
            user=self.user,
            event_key=NotificationPreference.Event.WEEKLY_DIGEST,
            email=True,
        )

    def test_a_digest_goes_out_when_there_is_enough_to_say(self):
        for index in range(3):
            _notification(self.user, title=f"Item {index}")

        self.assertTrue(digests.send_to(user=self.user))

        message = EmailMessage.objects.get()
        self.assertEqual(message.template_key, "digest.weekly")
        self.assertEqual(message.status, EmailStatus.SENT)

    def test_one_unread_item_is_not_worth_an_email(self):
        # It is already visible on the bell.
        _notification(self.user)
        self.assertFalse(digests.send_to(user=self.user))
        self.assertFalse(EmailMessage.objects.exists())

    def test_nothing_unread_sends_nothing(self):
        self.assertFalse(digests.send_to(user=self.user))

    def test_read_items_are_not_included(self):
        for index in range(3):
            _notification(self.user, title=f"Item {index}")
        Notification.objects.update(read_at="2026-01-01T00:00:00Z")

        self.assertFalse(digests.send_to(user=self.user))

    def test_a_suppressed_address_gets_no_digest(self):
        for index in range(3):
            _notification(self.user, title=f"Item {index}")
        channels.suppress(email="reader@example.test", reason=SuppressionReason.BOUNCE)

        self.assertFalse(digests.send_to(user=self.user))
        self.assertFalse(EmailMessage.objects.exists())

    def test_a_muted_group_stays_out_of_the_digest_too(self):
        # Otherwise the digest becomes the loophole that undoes muting.
        for index in range(3):
            _notification(self.user, title=f"Item {index}")
        NotificationPreference.objects.create(
            user=self.user, event_key="application_updates", in_app=False
        )

        self.assertFalse(digests.send_to(user=self.user))

    def test_a_collapsed_row_says_how_many_it_stands_for(self):
        _notification(self.user, count=7, title="Seven applications")
        _notification(self.user, title="Something else")

        digests.send_to(user=self.user)
        # Composed from the notifications themselves, never paraphrased.
        self.assertIn("(7x)", digests._compose(list(Notification.objects.all())))

    def test_the_run_only_reaches_people_who_asked_for_one(self):
        for index in range(3):
            _notification(self.user, title=f"Item {index}")

        opted_out = create_user(email="quiet@example.test")
        for index in range(3):
            _notification(opted_out, title=f"Item {index}")

        result = digests.run()

        self.assertEqual(result["considered"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(EmailMessage.objects.filter(user=opted_out).count(), 0)


class AudienceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        self.application = submit(job=self.job, profile=self.profile)

    def _event(self, type_: str) -> OutboxEvent:
        return OutboxEvent.objects.filter(type=type_).first()

    def test_an_unregistered_audience_raises_rather_than_returning_nobody(self):
        # Silence would look exactly like "nobody matched", which is the one
        # failure a delivery system must never hide.
        with self.assertRaises(audiences.UnknownAudience):
            audiences.resolve("no_such_audience", self._event("application.submitted"))

    def test_registering_the_same_key_twice_is_refused(self):
        with self.assertRaises(ValueError):

            @audiences.register("company_team")
            def _other(event):  # pragma: no cover - never called
                return None

    def test_the_actor_audience_is_deliberately_nobody(self):
        event = self._event("application.submitted")
        self.assertEqual(audiences.resolve("event_actor", event).count(), 0)

    def test_the_application_candidate_audience_is_the_applicant(self):
        event = self._event("application.submitted")
        recipients = list(audiences.resolve("application_candidate", event))
        self.assertEqual(recipients, [self.profile.user])

    def test_the_company_team_audience_is_the_people_who_can_open_it(self):
        # Decision D-13: Owner and Company Admin always; a recruiter only when
        # assigned to the job; roles whose link would refuse them, never.
        admin = create_user(email="admin@example.test")
        add_member(self.company, admin, Role.ADMIN)
        recruiter = create_user(email="colleague@example.test")
        add_member(self.company, recruiter, Role.RECRUITER)
        viewer = create_user(email="viewer@example.test")
        add_member(self.company, viewer, Role.VIEWER)

        event = self._event("application.submitted")
        recipients = set(audiences.resolve("application_company_team", event))

        self.assertEqual(recipients, {self.owner, admin})

    def test_assignment_adds_the_recruiter(self):
        colleague = create_user(email="colleague@example.test")
        membership = add_member(self.company, colleague, Role.RECRUITER)

        from apps.jobs.services import postings

        postings.assign(job=self.job, actor=self.owner, membership=membership)

        event = self._event("application.submitted")
        recipients = set(audiences.resolve("application_company_team", event))

        self.assertEqual(recipients, {self.owner, colleague})

    def test_a_company_audience_with_no_company_id_is_empty(self):
        orphan = OutboxEvent.objects.create(type="job.published", schema_version=1, payload={})
        self.assertEqual(audiences.resolve("company_team", orphan).count(), 0)

    def test_a_missing_application_resolves_to_nobody(self):
        orphan = OutboxEvent.objects.create(
            type="application.submitted",
            schema_version=1,
            payload={"application_public_id": "00000000-0000-0000-0000-000000000000"},
        )
        self.assertEqual(audiences.resolve("application_company_team", orphan).count(), 0)

    def test_the_conversation_audience_is_the_other_side(self):
        from apps.messaging.models import SenderKind
        from apps.messaging.services import conversations

        conversation = conversations.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
        )
        conversations.send(
            conversation=conversation,
            actor=self.owner,
            sender_kind=SenderKind.COMPANY,
            body="Hello",
        )

        event = OutboxEvent.objects.filter(type="message.sent").first()
        recipients = list(audiences.resolve("conversation_other_side", event))

        # The company sent it, so the candidate receives it.
        self.assertEqual(recipients, [self.profile.user])

    def test_a_candidate_message_reaches_the_company_team(self):
        from apps.messaging.models import SenderKind
        from apps.messaging.services import conversations

        conversation = conversations.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
        )
        OutboxEvent.objects.filter(type="message.sent").delete()
        conversations.send(
            conversation=conversation,
            actor=self.profile.user,
            sender_kind=SenderKind.CANDIDATE,
            body="Hello back",
        )

        event = OutboxEvent.objects.filter(type="message.sent").first()
        self.assertEqual(list(audiences.resolve("conversation_other_side", event)), [self.owner])

    def test_a_missing_conversation_resolves_to_nobody(self):
        orphan = OutboxEvent.objects.create(
            type="message.sent",
            schema_version=1,
            payload={"conversation_public_id": "00000000-0000-0000-0000-000000000000"},
        )
        self.assertEqual(audiences.resolve("conversation_other_side", orphan).count(), 0)

    def test_the_saved_job_audience_is_everyone_who_bookmarked_it(self):
        from apps.jobs.services import discovery

        discovery.save_job(profile=self.profile, actor=self.profile.user, job=self.job)
        event = OutboxEvent.objects.create(
            type="job.closed",
            schema_version=1,
            payload={"job_public_id": str(self.job.public_id), "company_id": self.company.pk},
        )

        recipients = list(audiences.resolve("saved_job_holders", event))
        self.assertEqual(recipients, [self.profile.user])

    def test_the_saved_search_audience_is_its_owner(self):
        event = OutboxEvent.objects.create(
            type="job.alert_matched",
            schema_version=1,
            payload={"profile_public_id": str(self.profile.public_id), "matches": 3},
        )
        self.assertEqual(list(audiences.resolve("saved_search_owner", event)), [self.profile.user])

    def test_an_inactive_user_is_never_an_audience(self):
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])

        event = self._event("application.submitted")
        self.assertNotIn(self.owner, audiences.resolve("application_company_team", event))
