"""
The in-app feed and its screen (SRS §13.5).

The exit criterion this file carries: **a muted event is provably never
delivered**. Two halves — the fan-out records it as muted and writes no row
(tested in `test_fanout`), and the feed excludes a group the person switched
off even if a row exists (tested here, because a row can exist from before they
muted it).
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.notifications.models import Notification, NotificationPreference
from apps.notifications.services import feed


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


class FeedTests(TestCase):
    def setUp(self):
        self.user = create_user()

    def test_the_feed_shows_this_persons_rows_only(self):
        _notification(self.user)
        _notification(create_user(email="other@example.test"), title="Not yours")

        titles = [item.title for item in feed.feed(self.user)]
        self.assertEqual(titles, ["New application"])

    def test_a_muted_group_is_filtered_out_entirely(self):
        # Not collected and hidden behind a badge: "turn this off" has to mean
        # off, or the count goes on rising for things somebody declined.
        _notification(self.user)
        NotificationPreference.objects.create(
            user=self.user, event_key="application_updates", in_app=False
        )

        self.assertEqual(feed.feed(self.user).count(), 0)
        self.assertEqual(feed.unread_count(self.user), 0)

    def test_muting_one_group_leaves_the_others(self):
        _notification(self.user)
        _notification(self.user, event_key="employer_messages", title="New message")
        NotificationPreference.objects.create(
            user=self.user, event_key="application_updates", in_app=False
        )

        titles = [item.title for item in feed.feed(self.user)]
        self.assertEqual(titles, ["New message"])

    def test_the_badge_costs_one_query(self):
        """Read by the application shell on every authenticated page (CV-04).

        Two queries here — the muted groups, then the count — is two queries on
        every page in the product, and two of the twenty-five NFR-2 allows one.
        """
        _notification(self.user)
        NotificationPreference.objects.create(
            user=self.user, event_key="application_updates", in_app=False
        )
        _notification(self.user, event_key="employer_messages", title="New message")

        with self.assertNumQueries(1):
            self.assertEqual(feed.unread_count(self.user), 1)

    def test_unread_count_ignores_read_rows(self):
        first = _notification(self.user)
        _notification(self.user, title="Second")
        feed.mark_read(user=self.user, public_id=first.public_id)

        self.assertEqual(feed.unread_count(self.user), 1)

    def test_marking_all_read_clears_the_count(self):
        for index in range(3):
            _notification(self.user, title=f"Item {index}")

        self.assertEqual(feed.mark_all_read(user=self.user), 3)
        self.assertEqual(feed.unread_count(self.user), 0)

    def test_reading_somebody_elses_row_does_nothing(self):
        theirs = _notification(create_user(email="other@example.test"))
        self.assertIsNone(feed.mark_read(user=self.user, public_id=theirs.public_id))

        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)

    def test_dismissing_removes_it_from_the_feed(self):
        notification = _notification(self.user)
        feed.dismiss(user=self.user, public_id=notification.public_id)

        self.assertEqual(feed.feed(self.user).count(), 0)

    def test_dismissing_is_undoable(self):
        # One click among a column of near-identical rows, and what it removes
        # exists nowhere else.
        notification = _notification(self.user)
        feed.dismiss(user=self.user, public_id=notification.public_id)
        feed.undismiss(user=self.user, public_id=notification.public_id)

        self.assertEqual(feed.feed(self.user).count(), 1)

    def test_the_undo_target_is_the_last_dismissal(self):
        first = _notification(self.user, title="First")
        second = _notification(self.user, title="Second")
        feed.dismiss(user=self.user, public_id=first.public_id)
        feed.dismiss(user=self.user, public_id=second.public_id)

        self.assertEqual(feed.last_dismissed(self.user).title, "Second")

    def test_dismissing_somebody_elses_row_does_nothing(self):
        theirs = _notification(create_user(email="other@example.test"))
        self.assertIsNone(feed.dismiss(user=self.user, public_id=theirs.public_id))


class ScreenTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.client.force_login(self.user)

    def test_the_list_renders(self):
        _notification(self.user)
        response = self.client.get(reverse("notifications:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New application")

    def test_the_filter_lists_each_kind_once(self):
        """`distinct()` after an ordering is not a distinct.

        The feed is ordered by arrival, and Postgres's `DISTINCT` considers
        every column in the `ORDER BY` — so three notifications of one kind
        produced three identical filter chips.
        """
        for index in range(3):
            _notification(self.user, title=f"Moved {index}", kind="application.stage_changed")

        response = self.client.get(reverse("notifications:index"))

        self.assertEqual(response.context["kinds"].count("application.stage_changed"), 1)

    def test_the_filter_reads_as_words_not_as_an_event_key(self):
        # `application.stage_changed` is how the system routes a notification.
        # It was printed verbatim on the candidate's own feed, as a filter chip
        # and beside every timestamp.
        _notification(self.user, kind="application.stage_changed")

        response = self.client.get(reverse("notifications:index"))
        text = response.content.decode()

        self.assertIn("Application stage changed", text)

    def test_signing_out_sends_you_to_sign_in(self):
        self.client.logout()
        response = self.client.get(reverse("notifications:index"))
        self.assertEqual(response.status_code, 302)

    def test_marking_read_from_the_screen_works(self):
        notification = _notification(self.user)

        self.client.post(
            reverse("notifications:act"),
            {"command": "read", "id": str(notification.public_id)},
        )

        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

    def test_dismiss_then_undo_from_the_screen(self):
        notification = _notification(self.user)
        act = reverse("notifications:act")

        self.client.post(act, {"command": "dismiss", "id": str(notification.public_id)})
        notification.refresh_from_db()
        self.assertIsNotNone(notification.dismissed_at)

        self.client.post(act, {"command": "undo", "id": str(notification.public_id)})
        notification.refresh_from_db()
        self.assertIsNone(notification.dismissed_at)

    def test_an_unknown_command_is_a_400(self):
        response = self.client.post(reverse("notifications:act"), {"command": "explode"})
        self.assertEqual(response.status_code, 400)

    def test_opening_marks_read_and_redirects_to_the_target(self):
        notification = _notification(self.user, target_url="/applications/")

        response = self.client.get(reverse("notifications:open", args=[notification.public_id]))

        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/applications/")

    def test_opening_somebody_elses_goes_back_to_the_list(self):
        theirs = _notification(create_user(email="other@example.test"))
        response = self.client.get(reverse("notifications:open", args=[theirs.public_id]))

        self.assertRedirects(response, reverse("notifications:index"))

    def test_the_bell_count_excludes_muted_groups(self):
        _notification(self.user)
        NotificationPreference.objects.create(
            user=self.user, event_key="application_updates", in_app=False
        )

        response = self.client.get(reverse("notifications:index"))
        self.assertEqual(response.context["unread"], 0)
