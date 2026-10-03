"""
The text interview screen, as a candidate uses it (FR-10.1, FR-10.4).

Each test is a defect reproduced on the running screen:

  * a refresh restarted the four-minute clock on the same question, and the
    answer's duration was measured from the reload;
  * an answer rejected with an error vanished with it;
  * pressing Start again opened another interview each time;
  * "Stop the interview" discarded the whole interview in one click, with no
    way to be scored on what had been answered;
  * a freshly booked interview's page said the company had not shared feedback;
  * a score whose quotation was struck out said nothing about it to the
    candidate.
"""

from __future__ import annotations

import datetime as dt

from django.urls import reverse
from django.utils import timezone

from apps.interviews.models import (
    InterviewEvaluation,
    InterviewSession,
    InterviewSlot,
    SessionState,
)
from apps.interviews.services import engine, evaluation, scheduling
from apps.interviews.tests.factories import live_template, window
from apps.interviews.tests.test_screens import ScreenFixture
from apps.matching.tests.test_matching import application_for


class TimerTests(ScreenFixture):
    def test_a_refresh_does_not_give_the_question_its_time_back(self):
        session = self.practice_session()
        url = reverse("interviews:session", args=[session.public_id])
        turn = self.client.get(url).context["turn"]

        type(turn).objects.filter(pk=turn.pk).update(
            started_at=timezone.now() - dt.timedelta(seconds=100)
        )
        seconds = self.client.get(url).context["seconds"]

        self.assertLessEqual(seconds, engine.ANSWER_SECONDS - 100)
        self.assertGreaterEqual(seconds, engine.ANSWER_SECONDS - 105)

    def test_the_clock_never_goes_below_zero(self):
        session = self.practice_session()
        url = reverse("interviews:session", args=[session.public_id])
        turn = self.client.get(url).context["turn"]
        type(turn).objects.filter(pk=turn.pk).update(
            started_at=timezone.now() - dt.timedelta(hours=1)
        )

        response = self.client.get(url)

        self.assertEqual(response.context["seconds"], 0)
        self.assertContains(response, "0:00")

    def test_duration_is_measured_from_when_the_question_was_shown(self):
        session = self.practice_session()
        url = reverse("interviews:session", args=[session.public_id])
        turn = self.client.get(url).context["turn"]
        type(turn).objects.filter(pk=turn.pk).update(
            started_at=timezone.now() - dt.timedelta(seconds=90)
        )

        self.client.post(url, {"turn": turn.pk, "answer": "I did it.", "duration_ms": "1"})

        turn.refresh_from_db()
        self.assertGreaterEqual(turn.duration_ms, 89_000)


class DraftTests(ScreenFixture):
    def test_a_rejected_answer_comes_back_with_the_error(self):
        session = self.practice_session()
        url = reverse("interviews:session", args=[session.public_id])
        turn = self.client.get(url).context["turn"]

        response = self.client.post(url, {"turn": turn.pk, "answer": "   "})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["draft"], "   ")
        self.assertContains(response, 'role="alert"', status_code=400)

    def test_the_draft_is_kept_per_question(self):
        session = self.practice_session()
        url = reverse("interviews:session", args=[session.public_id])
        response = self.client.get(url)

        self.assertContains(response, f"interview-draft-{response.context['turn'].pk}")


class ResumeTests(ScreenFixture):
    def test_pressing_start_again_resumes_rather_than_opening_another(self):
        url = reverse("interviews:practice")
        first = self.client.post(url, {"template": self.template.slug})
        second = self.client.post(url, {"template": self.template.slug})

        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(
            InterviewSession.objects.filter(
                candidate=self.profile, state=SessionState.IN_PROGRESS
            ).count(),
            1,
        )

    def test_practising_for_a_different_role_starts_a_new_one(self):
        url = reverse("interviews:practice")
        self.client.post(url, {"template": self.template.slug})
        self.client.post(url, {"template": self.template.slug, "job": str(self.job.public_id)})

        self.assertEqual(
            InterviewSession.objects.filter(
                candidate=self.profile, state=SessionState.IN_PROGRESS
            ).count(),
            2,
        )

    def test_a_finished_practice_is_not_resumed(self):
        url = reverse("interviews:practice")
        self.client.post(url, {"template": self.template.slug})
        session = InterviewSession.objects.get(candidate=self.profile)
        engine.abandon(session=session)

        self.client.post(url, {"template": self.template.slug})

        self.assertEqual(InterviewSession.objects.filter(candidate=self.profile).count(), 2)


class StoppingTests(ScreenFixture):
    def answer_one(self, session):
        url = reverse("interviews:session", args=[session.public_id])
        turn = self.client.get(url).context["turn"]
        self.client.post(url, {"turn": turn.pk, "answer": "I rebuilt the job."})

    def test_stopping_offers_a_choice_rather_than_discarding_on_one_click(self):
        session = self.practice_session()
        self.answer_one(session)

        response = self.client.get(reverse("interviews:session", args=[session.public_id]))

        self.assertContains(response, "<details")
        self.assertContains(response, "Finish now and get feedback on 1 answer")
        self.assertContains(response, "Discard this interview")

    def test_finish_now_is_not_offered_before_anything_is_answered(self):
        session = self.practice_session()
        response = self.client.get(reverse("interviews:session", args=[session.public_id]))
        self.assertNotContains(response, "Finish now")

    def test_finishing_after_answering_is_scored(self):
        session = self.practice_session()
        self.answer_one(session)

        self.client.post(
            reverse("interviews:finish", args=[session.public_id]), {"action": "finish"}
        )

        session.refresh_from_db()
        self.assertEqual(session.state, SessionState.COMPLETED)

    def test_finishing_with_nothing_answered_does_not_wait_forever_for_a_score(self):
        session = self.practice_session()
        self.client.get(reverse("interviews:session", args=[session.public_id]))

        self.client.post(
            reverse("interviews:finish", args=[session.public_id]), {"action": "finish"}
        )

        session.refresh_from_db()
        self.assertEqual(session.state, SessionState.ABANDONED)


class ResultWordingTests(ScreenFixture):
    def test_a_booked_interview_says_when_rather_than_no_feedback(self):
        application = application_for(self.profile, self.job)
        scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[window(hours_ahead=30)]
        )
        session = scheduling.book(
            slot=InterviewSlot.objects.latest("pk"),
            application=application,
            actor=self.profile.user,
            template=live_template(),
        )

        response = self.client.get(reverse("interviews:result", args=[session.public_id]))

        self.assertContains(response, "Booked for")
        self.assertNotContains(response, "has not shared feedback")

    def test_a_score_with_a_struck_out_quotation_says_so(self):
        session = self.practice_session()
        session.state = SessionState.COMPLETED
        session.save(update_fields=["state", "updated_at"])
        InterviewEvaluation.objects.create(
            session=session,
            competency="ownership",
            score=3,
            rationale="Said [not found in the answers].",
            grounded=False,
            submitted_at=timezone.now(),
        )
        evaluation.conclude(session=session)

        response = self.client.get(reverse("interviews:result", args=[session.public_id]))

        self.assertContains(response, "quotations were removed")


class AccessibilityTests(ScreenFixture):
    def test_the_question_is_the_pages_heading(self):
        session = self.practice_session()
        response = self.client.get(reverse("interviews:session", args=[session.public_id]))
        turn = response.context["turn"]

        self.assertContains(
            response,
            f'<h1 class="mt-2 text-title font-normal" id="question">{turn.question_text}</h1>',
            html=False,
        )
        self.assertContains(response, 'aria-describedby="question answer-hint"')
