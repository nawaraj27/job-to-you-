"""
Notification centre and preference defects reproduced on the running product
(section loop, 2026-09-14).

  * "Mark all read" on a list filtered to one kind cleared every unread
    notification, including kinds the person had not looked at;
  * an employer's preferences screen described candidate events — "Messages
    from employers", "New job matches" — although `WORDING` had an employer
    column nobody read;
  * the screen promised that interview times and withdrawn offers are "sent
    regardless". They belong to Application updates, and a switched-off group is
    never delivered (FR-11.1), so the promise was false.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.notifications import events
from apps.notifications.models import Notification
from apps.notifications.services import feed as feed_service
from apps.notifications.services import preferences as preference_service


def note(user, kind, group="application_updates", read=False):
    return Notification.objects.create(
        recipient=user,
        kind=kind,
        event_key=group,
        title=f"{kind} happened",
        target_url="/applications/",
        read_at=timezone.now() if read else None,
    )


class FilteredMarkAllReadTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.stage = note(self.user, "application.stage_changed")
        self.interview = note(self.user, "interview.scheduled")
        self.message = note(self.user, "message.sent", group="employer_messages")

    def test_the_service_reads_only_the_filtered_kind(self):
        changed = feed_service.mark_all_read(user=self.user, kind="interview.scheduled")

        self.assertEqual(changed, 1)
        self.assertEqual(
            set(
                Notification.objects.filter(recipient=self.user, read_at__isnull=True).values_list(
                    "kind", flat=True
                )
            ),
            {"application.stage_changed", "message.sent"},
        )

    def test_the_screen_passes_its_filter_through(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse("notifications:act"), {"command": "read_all", "kind": "message.sent"}
        )

        self.message.refresh_from_db()
        self.stage.refresh_from_db()
        self.assertIsNotNone(self.message.read_at)
        self.assertIsNone(self.stage.read_at)

    def test_without_a_filter_everything_visible_is_read(self):
        self.assertEqual(feed_service.mark_all_read(user=self.user), 3)

    def test_another_person_s_rows_are_untouched(self):
        other = create_user()
        theirs = note(other, "interview.scheduled")
        feed_service.mark_all_read(user=self.user, kind="interview.scheduled")
        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)


class PreferenceWordingTests(TestCase):
    def setUp(self):
        self.url = reverse("settings:notifications")

    def test_a_candidate_reads_candidate_wording(self):
        user = create_profile().user
        self.client.force_login(user)

        page = self.client.get(self.url)
        self.assertContains(page, "Messages from employers")
        self.assertNotContains(page, "New applicants")

    def test_an_employer_reads_employer_wording(self):
        user = create_user()
        company_with_owner(user)
        self.client.force_login(user)

        page = self.client.get(self.url)
        self.assertContains(page, "New applicants")
        self.assertContains(page, "Candidate replies")
        self.assertNotContains(page, "Messages from employers")
        self.assertNotContains(page, "New job matches")

    def test_someone_on_both_sides_reads_both(self):
        user = create_profile().user
        company_with_owner(user)
        self.client.force_login(user)

        page = self.client.get(self.url)
        self.assertContains(page, "Application updates · New applicants")
        self.assertContains(page, "As a job seeker:")
        self.assertEqual(preference_service.role_for(user), "both")

    def test_the_confirmation_uses_the_same_wording(self):
        user = create_user()
        company_with_owner(user)
        self.client.force_login(user)

        response = self.client.post(
            reverse("settings:notifications_act"),
            {
                "command": "channel",
                "event": "application_updates",
                "channel": "email",
                "on": "false",
            },
        )
        self.assertContains(response, "New applicants email: off")

    def test_opening_the_screen_does_not_make_an_employer_a_candidate(self):
        from apps.candidates.models import CandidateProfile

        user = create_user()
        company_with_owner(user)
        self.client.force_login(user)
        self.client.get(self.url)

        self.assertFalse(CandidateProfile.objects.filter(user=user).exists())


class FeedFilterSemanticsTests(TestCase):
    """axe critical `aria-allowed-attr`: `aria-selected` on plain links.

    `aria-selected` belongs to options, tabs and grid cells; on an `<a>` inside a
    `role="group"` a screen reader ignores it, so the current filter was never
    announced. `aria-current="page"` is the attribute for "this link is where
    you are", and the segmented style already reads it.
    """

    def test_the_active_filter_is_marked_current_not_selected(self):
        user = create_user()
        note(user, "interview.scheduled")
        self.client.force_login(user)

        page = self.client.get(reverse("notifications:index"), {"kind": "interview.scheduled"})
        html = " ".join(page.content.decode().split())
        # Only the filter row: the navigation rail marks its own active link.
        start = html.index('aria-label="Filter notifications"')
        chips = html[start : html.index("</div>", start)]

        self.assertNotIn("aria-selected", chips)
        self.assertEqual(chips.count('aria-current="page"'), 1)
        self.assertIn('href="?kind=interview.scheduled" aria-current="page"', chips)


class MutingCopyTests(TestCase):
    def test_interview_and_application_notices_are_mutable_so_the_page_says_so(self):
        groups = {
            spec.preference_group
            for key, spec in events.registry().items()
            if key.startswith(("interview.", "application.", "offer.")) and spec.audience
        }
        self.assertEqual(groups, {"application_updates"})

        self.client.force_login(create_profile().user)
        page = self.client.get(reverse("settings:notifications"))
        self.assertNotContains(page, "sent regardless")
        self.assertContains(page, "If you switch both of its channels off")
