"""
Notification delivery defects reproduced on the running product (section loop,
2026-09-14).

  * the weekly run took the first thousand preference rows, unordered, so
    everyone past them never received a digest;
  * running the weekly job twice (a Celery retry) sent everybody a second copy;
  * deactivated and erased accounts still received digests at their tombstone
    address;
  * the digest ignored the daily email cap its docstring promised;
  * the preferences screen said the digest comes on Sunday; it is sent Monday;
  * quiet hours (FR-11.4) were honoured by delivery and could not be set.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.notifications.events import EventSpec
from apps.notifications.models import (
    DeliveryStatus,
    EmailMessage,
    EmailStatus,
    Notification,
    NotificationPreference,
    OutboxEvent,
)
from apps.notifications.services import channels, digests
from apps.notifications.services import preferences as preference_service


def reader(**overrides):
    user = create_user(**overrides)
    NotificationPreference.objects.create(
        user=user, event_key=NotificationPreference.Event.WEEKLY_DIGEST, email=True
    )
    for index in range(3):
        Notification.objects.create(
            recipient=user,
            kind="application.stage_changed",
            event_key="application_updates",
            title=f"Update {index}",
        )
    return user


def digests_for(user) -> int:
    return EmailMessage.objects.filter(user=user, template_key="digest.weekly").count()


class DigestRunTests(TestCase):
    def test_every_opted_in_reader_is_reached_however_small_the_chunk(self):
        readers = [reader() for _ in range(5)]

        result = digests.run(chunk_size=2)

        self.assertEqual(result, {"considered": 5, "sent": 5})
        self.assertTrue(all(digests_for(user) == 1 for user in readers))

    def test_a_second_run_in_the_same_week_sends_nothing(self):
        user = reader()
        digests.run()
        result = digests.run()

        self.assertEqual(result["sent"], 0)
        self.assertEqual(digests_for(user), 1)

    def test_last_week_s_digest_does_not_block_this_week_s(self):
        user = reader()
        digests.run()
        EmailMessage.objects.filter(user=user).update(
            created_at=timezone.now() - dt.timedelta(days=7)
        )

        self.assertTrue(digests.send_to(user=user))
        self.assertEqual(digests_for(user), 2)

    def test_a_deactivated_account_gets_no_digest(self):
        user = reader(email="erased-1@tombstone.invalid")
        type(user).objects.filter(pk=user.pk).update(is_active=False)

        self.assertEqual(digests.run()["considered"], 0)
        self.assertFalse(digests.send_to(user=user))
        self.assertEqual(digests_for(user), 0)

    def test_the_daily_cap_applies_to_the_digest(self):
        user = reader()
        for _ in range(channels.EMAIL_CAP_PER_DAY):
            EmailMessage.objects.create(
                user=user, to_email=user.email, template_key="x", params={}, status=EmailStatus.SENT
            )

        self.assertFalse(digests.send_to(user=user))
        self.assertEqual(digests_for(user), 0)


class DigestDayCopyTests(TestCase):
    def test_the_screen_names_the_day_the_job_runs(self):
        from config.celery import app

        schedule = app.conf.beat_schedule["send-weekly-digests"]["schedule"]
        self.assertEqual(sorted(schedule.day_of_week), [1])

        self.client.force_login(create_profile().user)
        page = self.client.get(reverse("settings:notifications"))
        self.assertContains(page, "on Monday morning")
        self.assertNotContains(page, "Sunday")


class QuietHoursTests(TestCase):
    def setUp(self):
        self.user = create_profile().user
        self.user.timezone = "Asia/Kathmandu"
        self.user.save(update_fields=["timezone"])

    def test_a_window_is_saved_on_every_group_in_the_person_s_timezone(self):
        window = preference_service.set_quiet_hours(
            user=self.user, actor=self.user, start="22:00", end="07:00"
        )

        self.assertEqual(window, {"from": "22:00", "to": "07:00", "tz": "Asia/Kathmandu"})
        rows = NotificationPreference.objects.filter(user=self.user)
        self.assertEqual(rows.count(), len(NotificationPreference.Event.values))
        self.assertTrue(all(row.quiet_hours == window for row in rows))

    def test_bad_or_equal_times_are_refused(self):
        for start, end in (("25:00", "07:00"), ("22", "07:00"), ("22:00", "22:00"), ("", "07:00")):
            with self.subTest(start=start, end=end), self.assertRaises(InvalidState):
                preference_service.set_quiet_hours(
                    user=self.user, actor=self.user, start=start, end=end
                )

    def test_clearing_removes_the_window(self):
        preference_service.set_quiet_hours(
            user=self.user, actor=self.user, start="22:00", end="07:00"
        )
        preference_service.set_quiet_hours(user=self.user, actor=self.user, start="", end="")
        self.assertEqual(preference_service.quiet_hours_for(self.user), {})

    def test_a_saved_window_holds_back_email(self):
        from zoneinfo import ZoneInfo

        # A window around "now" in the person's zone, so the test holds at any
        # minute of the day (wrapping midnight is supported).
        now = timezone.now().astimezone(ZoneInfo("Asia/Kathmandu"))
        start = (now - dt.timedelta(hours=1)).strftime("%H:%M")
        end = (now + dt.timedelta(hours=1)).strftime("%H:%M")
        preference_service.set_quiet_hours(user=self.user, actor=self.user, start=start, end=end)
        event = OutboxEvent.objects.create(type="application.stage_changed", payload={})
        spec = EventSpec(
            type="application.stage_changed",
            schema_version=1,
            preference_group="application_updates",
            title="Update",
        )

        status, reason = channels.deliver_email(event=event, spec=spec, recipient=self.user)

        self.assertEqual((status, reason), (DeliveryStatus.MUTED, "quiet hours"))

    def test_the_screen_sets_and_clears_quiet_hours(self):
        self.client.force_login(self.user)
        act = reverse("settings:notifications_act")

        page = self.client.get(reverse("settings:notifications"))
        self.assertContains(page, 'name="quiet_from"')
        self.assertContains(page, "not sent afterwards either")

        response = self.client.post(
            act, {"command": "quiet_hours", "quiet_from": "21:30", "quiet_to": "06:30"}
        )
        self.assertContains(response, "Quiet hours: 21:30 to 06:30")
        self.assertEqual(preference_service.quiet_hours_for(self.user)["from"], "21:30")

        response = self.client.post(act, {"command": "quiet_clear"})
        self.assertContains(response, "Quiet hours switched off")
        self.assertEqual(preference_service.quiet_hours_for(self.user), {})

    def test_a_refused_window_shows_the_reason(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("settings:notifications_act"),
            {"command": "quiet_hours", "quiet_from": "22:00", "quiet_to": "22:00"},
        )
        self.assertContains(response, "different start and end time")
        self.assertEqual(preference_service.quiet_hours_for(self.user), {})
