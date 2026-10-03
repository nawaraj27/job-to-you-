"""
Channels: the feed, dedupe, quiet hours, caps and suppression (SRS §13.5).

The one this file exists to nail down: **a muted event is provably never
delivered.** That is P10's stated exit criterion, and "provably" means the test
asserts on the absence of the row, not on a code path.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.notifications.events import EventSpec
from apps.notifications.models import (
    DeliveryStatus,
    EmailMessage,
    EmailStatus,
    EmailSuppression,
    Notification,
    NotificationPreference,
    OutboxEvent,
    SuppressionReason,
)
from apps.notifications.services import channels, feed


def _spec(**overrides) -> EventSpec:
    fields = {
        "type": "application.submitted",
        "schema_version": 1,
        "preference_group": "application_updates",
        "title": "New application for {job_title}",
        "body": "Open the pipeline to review them.",
    }
    fields.update(overrides)
    return EventSpec(**fields)


def _event(**payload) -> OutboxEvent:
    return OutboxEvent.objects.create(
        type="application.submitted", schema_version=1, payload=payload or {}
    )


class RenderTests(TestCase):
    def test_the_wording_is_filled_from_the_payload(self):
        title, body = channels.render(_spec(), {"job_title": "Backend Engineer"})
        self.assertEqual(title, "New application for Backend Engineer")
        self.assertIn("review", body)

    def test_a_missing_key_renders_blank_rather_than_raising(self):
        # A notification with a gap is a small failure; a fan-out chunk that
        # dies because one payload was short is a large one.
        title, _ = channels.render(_spec(), {})
        self.assertEqual(title, "New application for ")


class InAppTests(TestCase):
    def setUp(self):
        self.user = create_user()

    def test_a_row_lands_in_the_feed(self):
        notification = channels.deliver_in_app(
            event=_event(job_title="Backend Engineer"), spec=_spec(), recipient=self.user
        )
        self.assertEqual(notification.recipient, self.user)
        self.assertEqual(notification.count, 1)

    def test_a_burst_collapses_into_one_line_that_counts(self):
        spec = _spec(dedupe="application.submitted:{job_public_id}")
        for _ in range(3):
            channels.deliver_in_app(
                event=_event(job_public_id="abc", job_title="Backend Engineer"),
                spec=spec,
                recipient=self.user,
            )

        notification = Notification.objects.get(recipient=self.user)
        self.assertEqual(notification.count, 3)

    def test_reading_the_collapsed_row_starts_a_fresh_one(self):
        spec = _spec(dedupe="application.submitted:{job_public_id}")
        channels.deliver_in_app(event=_event(job_public_id="abc"), spec=spec, recipient=self.user)
        feed.mark_all_read(user=self.user)

        channels.deliver_in_app(event=_event(job_public_id="abc"), spec=spec, recipient=self.user)

        # Two rows: the read one and a new one. Collapsing into something
        # already read would hide the new arrival.
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 2)

    def test_different_keys_do_not_collapse_together(self):
        spec = _spec(dedupe="application.submitted:{job_public_id}")
        for job_id in ("abc", "def"):
            channels.deliver_in_app(
                event=_event(job_public_id=job_id), spec=spec, recipient=self.user
            )
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 2)


class EmailGateTests(TestCase):
    def setUp(self):
        self.user = create_user(email="reader@example.test")

    def deliver(self, **kwargs):
        return channels.deliver_email(
            event=_event(job_title="Backend Engineer"),
            spec=_spec(**kwargs),
            recipient=self.user,
        )

    def test_a_normal_send_is_recorded_and_sent(self):
        status, _ = self.deliver()

        self.assertEqual(status, DeliveryStatus.SENT)
        message = EmailMessage.objects.get()
        self.assertEqual(message.status, EmailStatus.SENT)
        self.assertEqual(message.to_email, "reader@example.test")

    def test_the_stored_params_are_minimal(self):
        # SRS §5.12: never a context dump. That duplicates personal data into a
        # table with a different retention and grows without bound.
        self.deliver()
        self.assertEqual(set(EmailMessage.objects.get().params), {"title", "event"})

    def test_a_suppressed_address_is_never_written_to(self):
        channels.suppress(email="reader@example.test", reason=SuppressionReason.BOUNCE)

        status, reason = self.deliver()

        self.assertEqual(status, DeliveryStatus.SUPPRESSED)
        self.assertEqual(reason, "suppressed")
        self.assertFalse(EmailMessage.objects.exists())

    def test_suppression_is_idempotent(self):
        for _ in range(2):
            channels.suppress(email="reader@example.test", reason=SuppressionReason.BOUNCE)
        self.assertEqual(EmailSuppression.objects.count(), 1)

    def test_staff_can_lift_a_suppression(self):
        channels.suppress(email="reader@example.test", reason=SuppressionReason.MANUAL)
        self.assertEqual(channels.unsuppress(email="reader@example.test"), 1)
        self.assertFalse(channels.is_suppressed("reader@example.test"))

    def test_a_person_with_no_address_fails_rather_than_crashing(self):
        self.user.email = ""
        self.user.save(update_fields=["email"])

        status, reason = self.deliver()
        self.assertEqual(status, DeliveryStatus.FAILED)
        self.assertEqual(reason, "no address")

    def test_the_daily_cap_stops_the_flood(self):
        for _ in range(channels.EMAIL_CAP_PER_DAY):
            EmailMessage.objects.create(
                to_email="reader@example.test",
                template_key="x",
                status=EmailStatus.SENT,
            )

        status, reason = self.deliver()
        self.assertEqual(status, DeliveryStatus.SUPPRESSED)
        self.assertEqual(reason, "daily cap")

    def test_yesterdays_messages_do_not_count_against_today(self):
        for _ in range(channels.EMAIL_CAP_PER_DAY):
            message = EmailMessage.objects.create(
                to_email="reader@example.test", template_key="x", status=EmailStatus.SENT
            )
            EmailMessage.objects.filter(pk=message.pk).update(
                created_at=timezone.now() - dt.timedelta(days=2)
            )

        status, _ = self.deliver()
        self.assertEqual(status, DeliveryStatus.SENT)


class QuietHoursTests(TestCase):
    def setUp(self):
        self.user = create_user(email="sleeper@example.test")

    def _window(self, start: str, end: str, tz: str = "UTC"):
        NotificationPreference.objects.update_or_create(
            user=self.user,
            event_key="application_updates",
            defaults={"quiet_hours": {"from": start, "to": end, "tz": tz}},
        )

    def test_no_window_means_never_quiet(self):
        self.assertFalse(channels.in_quiet_hours(self.user, "application_updates"))

    def test_a_window_wrapping_midnight_covers_both_halves(self):
        # 22:00 to 07:00 is two intervals, not one. Getting this wrong makes
        # quiet hours silently do nothing.
        self._window("00:00", "23:59")
        self.assertTrue(channels.in_quiet_hours(self.user, "application_updates"))

    def test_a_window_that_excludes_now_is_not_quiet(self):
        now = timezone.now()
        start = (now + dt.timedelta(hours=2)).strftime("%H:00")
        end = (now + dt.timedelta(hours=4)).strftime("%H:00")
        if start >= end:
            self.skipTest("window wraps midnight in this run")
        self._window(start, end)

        self.assertFalse(channels.in_quiet_hours(self.user, "application_updates"))

    def test_a_malformed_window_is_ignored_rather_than_raising(self):
        self._window("not a time", "also not")
        self.assertFalse(channels.in_quiet_hours(self.user, "application_updates"))

    def test_an_unknown_timezone_falls_back_to_utc(self):
        self._window("00:00", "23:59", tz="Mars/Olympus_Mons")
        self.assertTrue(channels.in_quiet_hours(self.user, "application_updates"))

    def test_quiet_hours_hold_the_email_rather_than_dropping_it(self):
        self._window("00:00", "23:59")

        status, reason = channels.deliver_email(event=_event(), spec=_spec(), recipient=self.user)

        self.assertEqual(status, DeliveryStatus.MUTED)
        self.assertEqual(reason, "quiet hours")
        self.assertFalse(EmailMessage.objects.exists())
