"""
Interview notifications, delivered — not merely emitted.

Every earlier test stopped at "an OutboxEvent was written". Driving the real
pipeline on the development database showed what happened next: the audience
query joined through a reverse relation that does not exist
(`InterviewParticipant.user` has `related_name="+"`), fan-out raised on every
interview event, and no candidate or interviewer was ever told that an
interview was booked, moved, cancelled or about to start.

These run the outbox drain and the fan-out exactly as the workers do, and look
at the notifications that land in each person's feed.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.ai.services import prompts
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.interviews.models import InterviewSlot
from apps.interviews.services import scheduling
from apps.interviews.tests.factories import live_template, window
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for
from apps.notifications.models import Notification, OutboxEvent
from apps.notifications.services import fanout, outbox


def deliver() -> None:
    for _ in range(10):
        if not outbox.drain(500):
            break
    for _ in range(10):
        if not fanout.run_due(limit=500).get("chunks"):
            break


class InterviewNotificationDeliveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        self.candidate = create_profile()
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = application_for(self.candidate, self.job)
        scheduling.publish_slots(
            membership=self.membership,
            actor=self.owner,
            windows=[window(hours_ahead=30), window(hours_ahead=60)],
        )
        slots = list(
            InterviewSlot.objects.filter(interviewer=self.membership).order_by("starts_at")
        )
        self.later = slots[1]
        self.session = scheduling.book(
            slot=slots[0],
            application=self.application,
            actor=self.candidate.user,
            template=live_template(),
        )

    def feed(self, user, kind):
        # `kind` is the event type; `event_key` holds the preference group.
        return Notification.objects.filter(recipient=user, kind=kind)

    def test_a_booking_reaches_the_candidate_and_the_interviewer(self):
        deliver()

        for user in (self.candidate.user, self.owner):
            with self.subTest(user=user.email):
                rows = self.feed(user, "interview.scheduled")
                self.assertEqual(rows.count(), 1)
                self.assertIn(self.job.title, rows.first().title)
                self.assertNotRegex(rows.first().body, r"\d{4}-\d{2}-\d{2}T")

    def test_no_interview_event_is_left_failing(self):
        scheduling.cancel(session=self.session, actor=self.owner, reason="Role on hold")
        deliver()

        failing = OutboxEvent.objects.filter(type__startswith="interview.").exclude(last_error="")
        self.assertEqual(list(failing.values_list("type", "last_error")), [])

    def test_a_move_reaches_both_sides(self):
        deliver()
        scheduling.reschedule(
            session=self.session, slot=self.later, actor=self.owner, reason="Clash"
        )
        deliver()

        for user in (self.candidate.user, self.owner):
            with self.subTest(user=user.email):
                self.assertEqual(self.feed(user, "interview.rescheduled").count(), 1)
                # One notice for one move: not "booked" again as well.
                self.assertEqual(self.feed(user, "interview.scheduled").count(), 1)

    def test_a_cancellation_reaches_both_sides_with_the_reason(self):
        deliver()
        scheduling.cancel(session=self.session, actor=self.owner, reason="Role on hold")
        deliver()

        for user in (self.candidate.user, self.owner):
            with self.subTest(user=user.email):
                row = self.feed(user, "interview.cancelled").first()
                self.assertIsNotNone(row)
                self.assertIn("Role on hold", row.body)

    def test_nobody_outside_the_interview_is_told(self):
        stranger = create_profile().user
        deliver()

        self.assertFalse(Notification.objects.filter(recipient=stranger).exists())
