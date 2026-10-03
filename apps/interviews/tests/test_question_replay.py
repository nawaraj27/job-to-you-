"""
An answered question can never be answered again (the Back-button defect).

Reproduced in the browser on 2026-09-28: an old question page — the one the Back
button restores, or a second tab — could be submitted after the interview had
moved on; the server refused it but re-rendered the old question as a live form
that could be sent again and again. Two submissions of the current question
arriving together were both accepted (one answer silently replaced the other),
and the two pages they landed on each created "the next question": the same
question, twice, as two turns.

The rule, enforced in `engine` under the session row lock: an answer is
accepted only for the current unanswered question of a running interview.
Everything here goes through the real endpoints the browser uses.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.candidates.tests.factories import create_profile
from apps.interviews.models import InterviewKind, InterviewSession, InterviewTurn
from apps.interviews.services import delivery, engine
from apps.interviews.tests.factories import fake_ai, practice_template
from apps.interviews.tests.test_screens import ScreenFixture
from apps.interviews.tests.test_voice import VoiceFixture, audio


class ReplayFixture(ScreenFixture):
    def setUp(self):
        super().setUp()
        self.session = self.practice_session()
        self.url = reverse("interviews:session", kwargs={"public_id": self.session.public_id})

    def get_question(self):
        with fake_ai():
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.context["turn"]

    def post_answer(self, turn, text, **extra):
        with fake_ai():
            return self.client.post(self.url, {"turn": str(turn.pk), "answer": text, **extra})

    def answers(self):
        return list(
            InterviewTurn.objects.filter(session=self.session)
            .order_by("order")
            .values_list("order", "answer_text")
        )


class NormalFlowTests(ReplayFixture):
    def test_answering_moves_to_the_next_question_and_the_first_is_closed(self):
        first = self.get_question()
        self.assertRedirects(
            self.post_answer(first, "Answer one."), self.url, fetch_redirect_response=False
        )

        second = self.get_question()
        self.assertEqual(second.order, 1)
        self.assertEqual(engine.current_turn(self.session).pk, second.pk)

    def test_the_question_page_is_never_stored_by_the_browser(self):
        with fake_ai():
            response = self.client.get(self.url)
        self.assertIn("no-store", response["Cache-Control"])


class BrowserBackTests(ReplayFixture):
    def test_get_answer_get_then_the_old_form_again(self):
        """GET Q1 → POST → GET Q2 → POST the Q1 form again."""
        first = self.get_question()
        self.post_answer(first, "Answer one.")
        second = self.get_question()

        response = self.post_answer(first, "A second answer to question one.")

        self.assertEqual(response.status_code, 409)
        self.assertTemplateUsed(response, "interviews/stale.html")
        self.assertContains(response, "That question has already been answered", status_code=409)
        self.assertContains(response, "Continue interview", status_code=409)
        self.assertContains(response, self.url, status_code=409)
        # Not the old question as a live form.
        self.assertNotContains(response, 'name="answer"', status_code=409)
        self.assertEqual(self.answers(), [(0, "Answer one."), (1, "")])
        self.assertEqual(engine.current_turn(self.session).pk, second.pk)
        self.assertEqual(self.get_question().pk, second.pk)

    def test_what_they_typed_is_shown_back_not_saved(self):
        first = self.get_question()
        self.post_answer(first, "Answer one.")
        self.get_question()

        response = self.post_answer(first, "Words I would not want to lose.")
        self.assertContains(response, "Words I would not want to lose.", status_code=409)
        self.assertContains(response, "not saved", status_code=409)

    def test_skipping_an_old_question_is_refused_too(self):
        first = self.get_question()
        self.post_answer(first, "Answer one.")
        self.get_question()

        with fake_ai():
            response = self.client.post(self.url, {"turn": str(first.pk), "action": "skip"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.answers()[0], (0, "Answer one."))

    def test_the_form_resubmitted_by_the_browser_is_still_refused(self):
        first = self.get_question()
        self.post_answer(first, "Answer one.")
        self.get_question()
        for _ in range(3):  # Back → submit, refresh → resubmit, retry
            self.assertEqual(self.post_answer(first, "Replay").status_code, 409)
        self.assertEqual(self.answers(), [(0, "Answer one."), (1, "")])


class RefreshTests(ReplayFixture):
    def test_refreshing_the_current_question_is_safe(self):
        first = self.get_question()
        again = self.get_question()
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(InterviewTurn.objects.filter(session=self.session).count(), 1)

        self.post_answer(first, "Answer one.")
        self.assertEqual(self.answers()[0], (0, "Answer one."))

    def test_refreshing_after_answering_shows_the_next_question_not_the_old_one(self):
        first = self.get_question()
        self.post_answer(first, "Answer one.")
        for _ in range(2):
            self.assertEqual(self.get_question().order, 1)
        self.assertEqual(InterviewTurn.objects.filter(session=self.session).count(), 2)


class DoubleSubmitTests(ReplayFixture):
    def test_the_same_answer_twice_is_one_answer_and_one_step(self):
        first = self.get_question()
        one = self.post_answer(first, "Answer one.")
        two = self.post_answer(first, "Answer one.")

        # The repeat carries the answer already recorded: it carries on, quietly.
        self.assertRedirects(one, self.url, fetch_redirect_response=False)
        self.assertRedirects(two, self.url, fetch_redirect_response=False)
        self.get_question()
        self.get_question()
        self.assertEqual(self.answers(), [(0, "Answer one."), (1, "")])

    def test_a_different_second_answer_never_replaces_the_first(self):
        first = self.get_question()
        self.post_answer(first, "Answer one.")
        response = self.post_answer(first, "Changed my mind.")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.answers()[0], (0, "Answer one."))


class MultipleTabTests(ReplayFixture):
    def test_tab_b_cannot_answer_what_tab_a_already_answered(self):
        tab_a, tab_b = self.client, Client()
        tab_b.force_login(self.profile.user)
        first = self.get_question()
        with fake_ai():
            self.assertEqual(tab_b.get(self.url).context["turn"].pk, first.pk)

        self.post_answer(first, "From tab A.")
        self.get_question()
        with fake_ai():
            response = tab_b.post(self.url, {"turn": str(first.pk), "answer": "From tab B."})

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Continue interview", status_code=409)
        self.assertEqual(self.answers(), [(0, "From tab A."), (1, "")])
        self.assertIs(tab_a, self.client)


class CompletedInterviewTests(ReplayFixture):
    def test_an_old_question_after_completion_changes_nothing_and_scores_nothing(self):
        first = self.get_question()
        self.post_answer(first, "Answer one.")
        with mock.patch.object(engine, "_queue_evaluation") as queued:
            self.client.post(
                reverse("interviews:finish", kwargs={"public_id": self.session.public_id}),
                {"action": "finish"},
            )
            self.session.refresh_from_db()
            self.assertTrue(self.session.is_finished)

            response = self.post_answer(first, "After the end.")
            engine.conclude(session=self.session)  # a second request reaching the end
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "This interview has already finished", status_code=409)
        self.assertContains(
            response,
            reverse("interviews:result", kwargs={"public_id": self.session.public_id}),
            status_code=409,
        )
        self.assertEqual(self.answers()[0], (0, "Answer one."))
        # The evaluation was queued once, by the finish, and never again.
        self.assertLessEqual(queued.call_count, 1)


class AuthorizationTests(ReplayFixture):
    def test_another_candidate_cannot_answer_this_interview(self):
        first = self.get_question()
        other = create_profile()
        self.client.force_login(other.user)
        response = self.post_answer(first, "Not mine.")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.answers(), [(0, "")])

    def test_a_question_from_another_interview_is_not_part_of_this_one(self):
        first = self.get_question()
        other_session = self.practice_session()
        with fake_ai():
            other_turn = engine.next_question(session=other_session)

        response = self.post_answer(other_turn, "Wrong interview.")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.answers(), [(0, "")])
        self.assertEqual(first.session_id, self.session.pk)


class VoiceReplayTests(VoiceFixture):
    def test_a_recording_for_an_old_question_is_not_transcribed(self):
        session = self.voice_session()
        first = self.current_turn(session)
        engine.submit_answer(session=session, turn=first, text="Spoken answer one.")
        self.current_turn(session)

        response = self.transcribe(session, first)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["stale"])

    def test_an_old_transcript_cannot_be_submitted_after_the_question_moved_on(self):
        session = self.voice_session()
        url = reverse("interviews:session", args=[session.public_id])
        first = self.current_turn(session)
        # A transcript and its sealed timings, made for question one.
        seal = delivery.sign({"words": 12, "seconds": 6.0})
        with fake_ai():
            self.client.post(url, {"turn": str(first.pk), "answer": "Spoken answer one."})
        second = self.current_turn(session)

        with fake_ai():
            response = self.client.post(
                url, {"turn": str(first.pk), "answer": "Old transcript again.", "delivery": seal}
            )
        self.assertEqual(response.status_code, 409)
        first.refresh_from_db()
        self.assertEqual(first.answer_text, "Spoken answer one.")
        self.assertEqual(engine.current_turn(session).pk, second.pk)

    def test_a_voice_answer_to_the_current_question_still_works(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        response = self.transcribe(session, turn, upload=audio())
        self.assertEqual(response.status_code, 200)


class ConcurrentSubmissionTests(TransactionTestCase):
    """Two submissions of the current question at the same moment.

    Real threads, real connections, real row locks: this is the race a double
    click or two tabs produce, and a test inside one transaction cannot see it.
    """

    def setUp(self):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_interview_templates", quiet_summary=True)
        prompts.load_all()
        FakeAdapter.reset()
        self.profile = create_profile()
        with fake_ai():
            self.session = engine.start(
                candidate=self.profile, template=practice_template(), actor=self.profile.user
            )
            self.turn = engine.next_question(session=self.session)
        self.url = reverse("interviews:session", kwargs={"public_id": self.session.public_id})

    def test_exactly_one_of_two_simultaneous_answers_is_accepted(self):
        start = threading.Barrier(2)
        statuses: list[int] = []

        def submit(text: str) -> None:
            client = Client()
            client.force_login(self.profile.user)
            start.wait()
            try:
                with fake_ai():
                    response = client.post(self.url, {"turn": str(self.turn.pk), "answer": text})
                statuses.append(response.status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=submit, args=(t,)) for t in ("First.", "Second.")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(sorted(statuses), [302, 409])
        self.turn.refresh_from_db()
        self.assertIn(self.turn.answer_text, ("First.", "Second."))

    def test_two_simultaneous_next_question_requests_create_one_question(self):
        with fake_ai():
            engine.submit_answer(session=self.session, turn=self.turn, text="Answer one.")
        start = threading.Barrier(2)
        seen: list[int] = []

        def load() -> None:
            client = Client()
            client.force_login(self.profile.user)
            start.wait()
            try:
                with fake_ai():
                    response = client.get(self.url)
                seen.append(response.context["turn"].pk)
            finally:
                connection.close()

        threads = [threading.Thread(target=load) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(InterviewTurn.objects.filter(session=self.session).count(), 2)
        self.assertEqual(
            InterviewSession.objects.get(pk=self.session.pk).kind, InterviewKind.AI_TEXT
        )
