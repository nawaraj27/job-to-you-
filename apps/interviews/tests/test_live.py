"""
A live interview from booking to done, from both sides (FR-10.5 to FR-10.10).

Every assertion here is a defect that was reproduced in the running product:

  * the employer's "Join the room" linked to a room *name*, so it 404'd;
  * nothing moved a live interview out of `scheduled`, so one that happened
    read "Scheduled" forever and never appeared under "Done";
  * the room closed fifteen minutes after the *start*, so somebody who dropped
    out of a forty-five minute call could not get back in;
  * a no-show could be recorded days before the interview;
  * the hub offered "Join" for days before the room opened, and "Continue" on
    an unfinished practice led to the result page instead of the interview;
  * notifications said "booked for 2026-09-15T04:15:00+00:00".
"""

from __future__ import annotations

import datetime as dt

from django.urls import reverse
from django.utils import timezone

from apps.interviews import policies
from apps.interviews.models import (
    InterviewParticipant,
    InterviewSession,
    InterviewSlot,
    SessionState,
)
from apps.interviews.services import evaluation, scheduling
from apps.interviews.tests.factories import live_template, window
from apps.interviews.tests.test_screens import ScreenFixture
from apps.matching.tests.test_matching import application_for
from apps.notifications.models import OutboxEvent


class LiveFixture(ScreenFixture):
    def setUp(self):
        super().setUp()
        self.application = application_for(self.profile, self.job)
        scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[window(hours_ahead=30)]
        )
        slot = InterviewSlot.objects.filter(interviewer=self.membership).latest("pk")
        self.session = scheduling.book(
            slot=slot,
            application=self.application,
            actor=self.profile.user,
            template=live_template(),
        )

    def starts_in(self, minutes: int, *, length: int = 45) -> None:
        """Move the booked interview so it starts `minutes` from now."""
        starts = timezone.now() + dt.timedelta(minutes=minutes)
        InterviewSlot.objects.filter(session=self.session).update(
            starts_at=starts, ends_at=starts + dt.timedelta(minutes=length)
        )
        InterviewSession.objects.filter(pk=self.session.pk).update(scheduled_at=starts)
        self.session.refresh_from_db()

    def employer_url(self, name: str) -> str:
        return reverse(f"employer:{name}", args=[self.company.slug, self.session.public_id])


class JoinWindowTests(LiveFixture):
    def test_the_room_is_closed_a_day_ahead(self):
        self.assertFalse(policies.can_join(self.session))

    def test_the_room_opens_fifteen_minutes_before(self):
        self.starts_in(10)
        self.assertTrue(policies.can_join(self.session))

    def test_somebody_who_dropped_out_can_rejoin_twenty_minutes_in(self):
        self.starts_in(-20)
        scheduling.record_join(session=self.session, user=self.profile.user)
        self.session.refresh_from_db()

        self.assertEqual(self.session.state, SessionState.IN_PROGRESS)
        self.assertTrue(policies.can_join(self.session))

    def test_the_room_closes_fifteen_minutes_after_the_end(self):
        self.starts_in(-61, length=45)
        self.assertFalse(policies.can_join(self.session))
        with self.assertRaisesMessage(Exception, "has passed"):
            policies.require_joinable(self.session)

    def test_a_cancelled_interview_says_so_rather_than_when_the_room_opens(self):
        self.starts_in(5)
        scheduling.cancel(session=self.session, actor=self.owner)
        self.session.refresh_from_db()

        with self.assertRaisesMessage(Exception, "cancelled"):
            policies.require_joinable(self.session)


class EmployerJoinTests(LiveFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_the_join_link_is_a_route_not_a_room_name(self):
        self.starts_in(5)
        response = self.client.get(self.employer_url("interview_detail"))

        self.assertContains(response, self.employer_url("interview_join"))
        self.assertNotContains(response, f'href="{self.session.room_ref}"')

    def test_joining_goes_to_the_providers_room_and_starts_the_interview(self):
        self.starts_in(5)
        response = self.client.get(self.employer_url("interview_join"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(self.session.room_ref, response["Location"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.IN_PROGRESS)
        self.assertTrue(
            InterviewParticipant.objects.get(session=self.session, user=self.owner).attended
        )

    def test_joining_before_the_room_opens_explains_and_changes_nothing(self):
        response = self.client.get(self.employer_url("interview_join"), follow=True)

        self.assertContains(response, "opens fifteen minutes before")
        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.SCHEDULED)

    def test_another_company_cannot_join(self):
        from apps.candidates.tests.factories import create_profile
        from apps.companies.tests.factories import company_with_owner

        self.starts_in(5)
        stranger = create_profile().user
        other, _ = company_with_owner(stranger)
        self.client.force_login(stranger)

        response = self.client.get(
            reverse("employer:interview_join", args=[other.slug, self.session.public_id])
        )

        self.assertEqual(response.status_code, 404)
        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.SCHEDULED)


class CandidateJoinTests(LiveFixture):
    def test_joining_in_the_window_shows_the_room_and_starts_the_interview(self):
        self.starts_in(5)
        response = self.client.get(reverse("interviews:join", args=[self.session.public_id]))

        self.assertContains(response, "Join the room")
        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.IN_PROGRESS)

    def test_another_candidate_cannot_open_the_room(self):
        from apps.candidates.tests.factories import create_profile

        self.starts_in(5)
        self.client.force_login(create_profile().user)

        response = self.client.get(reverse("interviews:join", args=[self.session.public_id]))

        self.assertEqual(response.status_code, 404)


class CompletionTests(LiveFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def score_and_conclude(self):
        self.client.post(
            self.employer_url("interview_detail"),
            {"action": "score", "competency": "problem_solving", "score": "4"},
        )
        return self.client.post(
            self.employer_url("interview_detail"), {"action": "conclude"}, follow=True
        )

    def test_recording_the_outcome_completes_a_live_interview(self):
        self.starts_in(-50)

        self.score_and_conclude()

        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.COMPLETED)
        self.assertIsNotNone(self.session.ended_at)
        self.assertTrue(
            OutboxEvent.objects.filter(
                type="interview.completed", payload__session_public_id=str(self.session.public_id)
            ).exists()
        )

    def test_a_completed_live_interview_is_listed_as_done(self):
        self.starts_in(-50)
        self.score_and_conclude()

        response = self.client.get(reverse("employer:interviews", args=[self.company.slug]))

        self.assertContains(response, "Done")
        self.assertIn(self.session, list(response.context["completed"]))

    def test_an_outcome_before_the_interview_happened_is_refused(self):
        response = self.score_and_conclude()

        self.assertContains(response, "has not happened yet")
        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.SCHEDULED)
        self.assertIsNone(evaluation.readable_by_candidate(self.session))

    def test_the_outcome_button_waits_for_a_scorecard(self):
        self.starts_in(-50)
        response = self.client.get(self.employer_url("interview_detail"))

        self.assertNotContains(response, "Record the outcome")
        self.assertContains(response, "Submit a scorecard to record an outcome")

    def test_a_no_show_cannot_be_recorded_before_the_interview_time(self):
        response = self.client.post(
            self.employer_url("interview_detail"),
            {"action": "no_show", "missing_side": "candidate"},
            follow=True,
        )

        self.assertContains(response, "once the interview time has arrived")
        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.SCHEDULED)

    def test_a_no_show_after_the_time_records_who_was_missing(self):
        self.starts_in(-20)
        self.client.post(
            self.employer_url("interview_detail"),
            {"action": "no_show", "missing_side": "interviewer"},
        )

        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.NO_SHOW)
        self.assertIn("interviewer", self.session.cancelled_reason)

    def test_an_interview_under_way_cannot_be_moved(self):
        from apps.common.errors import InvalidState

        self.starts_in(-5)
        scheduling.record_join(session=self.session, user=self.owner)
        scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[window(hours_ahead=80)]
        )
        free = InterviewSlot.objects.filter(state="open").latest("pk")
        self.session.refresh_from_db()

        with self.assertRaises(InvalidState):
            scheduling.reschedule(session=self.session, slot=free, actor=self.owner)


class HubTests(LiveFixture):
    def test_join_is_not_offered_days_before_the_room_opens(self):
        response = self.client.get(reverse("interviews:hub"))

        self.assertNotContains(response, "Join now")
        self.assertContains(response, "Join opens 15 minutes before")

    def test_join_is_offered_when_the_room_is_open(self):
        self.starts_in(5)
        self.assertContains(self.client.get(reverse("interviews:hub")), "Join now")

    def test_an_interview_under_way_stays_on_the_hub(self):
        self.starts_in(-20)
        scheduling.record_join(session=self.session, user=self.profile.user)

        response = self.client.get(reverse("interviews:hub"))

        self.assertIn(self.session, list(response.context["upcoming"]))
        self.assertContains(response, "Join now")

    def test_continue_on_an_unfinished_practice_goes_back_into_it(self):
        practice = self.practice_session()

        response = self.client.get(reverse("interviews:hub"))

        self.assertContains(response, reverse("interviews:session", args=[practice.public_id]))


class NotificationWordingTests(LiveFixture):
    def test_the_booking_notification_reads_as_a_time_and_names_the_role(self):
        event = OutboxEvent.objects.filter(
            type="interview.scheduled", payload__session_public_id=str(self.session.public_id)
        ).latest("pk")

        when = event.payload["when"]
        self.assertNotRegex(when, r"\d{4}-\d{2}-\d{2}T")
        self.assertEqual(event.payload["job"], self.job.title)

    def test_the_wording_does_not_claim_the_company_booked_it(self):
        from apps.notifications.events import spec_for
        from apps.notifications.services.channels import render

        event = OutboxEvent.objects.filter(type="interview.scheduled").latest("pk")
        title, body = render(spec_for("interview.scheduled"), event.payload)

        self.assertNotIn("has scheduled your", body)
        self.assertIn(self.job.title, title)


class PublishFormTests(ScreenFixture):
    def test_several_times_can_be_published_in_one_go(self):
        self.client.force_login(self.owner)
        day = (timezone.localdate() + dt.timedelta(days=3)).isoformat()

        response = self.client.post(
            reverse("employer:interview_slots", args=[self.company.slug]),
            {"date": day, "time": ["09:00", "10:00", "", "14:00"], "minutes": "45"},
            follow=True,
        )

        self.assertContains(response, "Published 3 times")
        self.assertEqual(InterviewSlot.objects.filter(interviewer=self.membership).count(), 3)

    def test_the_form_offers_more_than_one_time_field(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("employer:interviews", args=[self.company.slug]))
        self.assertGreaterEqual(response.content.decode().count('name="time"'), 4)


class StaleLiveSessionTests(LiveFixture):
    """The sweep marked a live interview that had happened as abandoned."""

    def test_a_live_interview_whose_room_has_closed_is_completed_not_abandoned(self):
        from apps.interviews.tasks import close_stale_sessions

        self.starts_in(-5 * 60)
        InterviewSession.objects.filter(pk=self.session.pk).update(
            state=SessionState.IN_PROGRESS, started_at=timezone.now() - dt.timedelta(hours=5)
        )

        self.assertEqual(close_stale_sessions(), 1)

        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.COMPLETED)
        self.assertTrue(
            OutboxEvent.objects.filter(
                type="interview.completed", payload__session_public_id=str(self.session.public_id)
            ).exists()
        )

    def test_a_completed_stale_live_interview_can_still_be_scored(self):
        from apps.interviews.tasks import close_stale_sessions

        self.starts_in(-8 * 60)
        InterviewSession.objects.filter(pk=self.session.pk).update(
            state=SessionState.IN_PROGRESS, started_at=timezone.now() - dt.timedelta(hours=8)
        )
        close_stale_sessions()
        self.client.force_login(self.owner)

        self.client.post(
            self.employer_url("interview_detail"),
            {"action": "score", "competency": "problem_solving", "score": "3"},
        )
        self.client.post(self.employer_url("interview_detail"), {"action": "conclude"})

        from apps.interviews.models import InterviewOutcome

        self.assertTrue(InterviewOutcome.objects.filter(session=self.session).exists())

    def test_a_live_interview_still_in_its_room_is_left_alone(self):
        from apps.interviews.tasks import close_stale_sessions

        self.starts_in(-20)
        scheduling.record_join(session=self.session, user=self.owner)

        self.assertEqual(close_stale_sessions(hours=0), 0)
        self.session.refresh_from_db()
        self.assertEqual(self.session.state, SessionState.IN_PROGRESS)
