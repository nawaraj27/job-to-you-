"""
The interview screens, both sides (FR-10.2, FR-10.8, FR-10.11, INV-6).

What is asserted here, and why each matters:

  * **the flag is the gate** — `interview` was off from P0 to P16 because none
    of this existed, and P16 is what makes it true;
  * **a practice session is unreachable from the employer portal**, which is
    the promise practice mode makes;
  * **another interviewer's unsubmitted scorecard never renders** — the
    anchoring rule, asserted through the screen rather than only the selector;
  * a candidate sees a result only when they are allowed to (FR-10.11).
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.candidates.tests.factories import create_profile
from apps.companies.models import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.core import features
from apps.core.models import FeatureFlag
from apps.interviews.models import (
    InterviewEvaluation,
    InterviewSession,
    SessionState,
)
from apps.interviews.services import engine, evaluation, scheduling
from apps.interviews.tests.factories import (
    PLAN_OUTPUT,
    fake_ai,
    live_template,
    practice_template,
    window,
)
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for


def disable_interviews(test) -> None:
    FeatureFlag.objects.update_or_create(
        key="interview",
        defaults={
            "is_enabled": False,
            "expires_on": timezone.localdate() + dt.timedelta(days=7),
        },
    )
    features.invalidate_cache()
    test.addCleanup(features.invalidate_cache)


class ScreenFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_interview_templates", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        self.profile = create_profile()
        self.template = practice_template()

        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))

        self.client.force_login(self.profile.user)

    def practice_session(self) -> InterviewSession:
        with fake_ai():
            return engine.start(
                candidate=self.profile, template=self.template, actor=self.profile.user
            )


class CandidateHubTests(ScreenFixture):
    def test_the_hub_is_a_404_when_the_flag_is_off(self):
        # INV-6: the flag was off from P0 to P16 because none of this existed.
        disable_interviews(self)
        self.assertEqual(self.client.get(reverse("interviews:hub")).status_code, 404)

    def test_the_hub_renders_with_the_flag_on(self):
        response = self.client.get(reverse("interviews:hub"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Practise")

    def test_the_hub_says_practice_is_private(self):
        response = self.client.get(reverse("interviews:hub"))
        self.assertContains(response, "no employer sees them")

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("interviews:hub")).status_code, 302)

    def test_starting_practice_creates_a_session_and_redirects(self):
        with fake_ai():
            response = self.client.post(
                reverse("interviews:practice"), {"template": "general-practice"}
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            InterviewSession.objects.filter(candidate=self.profile, application=None).exists()
        )

    def test_an_unknown_template_is_a_404(self):
        response = self.client.post(reverse("interviews:practice"), {"template": "nonsense"})
        self.assertEqual(response.status_code, 404)


class SessionScreenTests(ScreenFixture):
    def test_the_screen_shows_one_question(self):
        session = self.practice_session()
        with fake_ai():
            response = self.client.get(
                reverse("interviews:session", kwargs={"public_id": session.public_id})
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your answer")
        self.assertEqual(response.context["turn"].order, 0)

    def test_the_screen_says_when_the_questions_are_not_the_roles_own(self):
        """A candidate picks a role and gets the general set, silently.

        The first person to practise against a new posting gets the four
        general competencies, because the posting's own kit is written off the
        request and nobody should wait thirty seconds for it. That is the right
        behaviour and it was invisible: eight questions that never mention the
        job, under a heading that asked which role they were practising for.
        """
        with fake_ai():
            session = engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                job=self.job,
            )

        with fake_ai():
            response = self.client.get(reverse("interviews:session", args=[session.public_id]))

        self.assertFalse(session.plan["tailored"])
        self.assertContains(response, "Built from the")
        self.assertContains(response, self.job.title)

    def test_a_session_on_the_roles_own_kit_says_nothing(self):
        from apps.interviews.services import planning

        with fake_ai():
            FakeAdapter.queue(PLAN_OUTPUT)
            planning.plan_job(job=self.job)

        with fake_ai():
            session = engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                job=self.job,
            )

        with fake_ai():
            response = self.client.get(reverse("interviews:session", args=[session.public_id]))

        self.assertTrue(session.plan["tailored"])
        self.assertNotContains(response, "Built from the")

    def test_the_screen_declares_what_is_recorded(self):
        # A candidate is entitled to know what is being recorded about them.
        session = self.practice_session()
        with fake_ai():
            response = self.client.get(
                reverse("interviews:session", kwargs={"public_id": session.public_id})
            )
        self.assertContains(response, "never part of your score")

    def test_submitting_an_answer_moves_to_the_next_question(self):
        session = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=session)
            response = self.client.post(
                reverse("interviews:session", kwargs={"public_id": session.public_id}),
                {"turn": turn.pk, "answer": "I rebuilt the importer.", "duration_ms": 1000},
            )

        self.assertEqual(response.status_code, 302)
        turn.refresh_from_db()
        self.assertEqual(turn.answer_text, "I rebuilt the importer.")

    def test_an_empty_answer_returns_the_same_question_with_an_error(self):
        session = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=session)
            response = self.client.post(
                reverse("interviews:session", kwargs={"public_id": session.public_id}),
                {"turn": turn.pk, "answer": "  "},
            )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Write an answer", status_code=400)

    def test_integrity_counts_are_parsed_from_the_form(self):
        session = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=session)
            self.client.post(
                reverse("interviews:session", kwargs={"public_id": session.public_id}),
                {
                    "turn": turn.pk,
                    "answer": "I did it.",
                    "integrity": '{"pastes": 2, "blur_events": 1, "keystrokes": 90}',
                },
            )

        turn.refresh_from_db()
        self.assertEqual(turn.integrity_meta["pastes"], 2)

    def test_unparseable_integrity_does_not_fail_the_answer(self):
        # Telemetry. A browser that sent nonsense must not fail an interview.
        session = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=session)
            response = self.client.post(
                reverse("interviews:session", kwargs={"public_id": session.public_id}),
                {"turn": turn.pk, "answer": "I did it.", "integrity": "not json"},
            )

        self.assertEqual(response.status_code, 302)
        turn.refresh_from_db()
        self.assertTrue(turn.is_answered)

    def test_a_turn_from_another_session_is_a_404(self):
        first = self.practice_session()
        second = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=first)
            response = self.client.post(
                reverse("interviews:session", kwargs={"public_id": second.public_id}),
                {"turn": turn.pk, "answer": "Wrong session."},
            )

        self.assertEqual(response.status_code, 404)

    def test_another_candidates_session_is_a_404(self):
        session = self.practice_session()
        self.client.force_login(create_profile().user)

        response = self.client.get(
            reverse("interviews:session", kwargs={"public_id": session.public_id})
        )
        self.assertEqual(response.status_code, 404)

    def test_a_finished_session_redirects_to_the_result(self):
        session = self.practice_session()
        engine.abandon(session=session)

        response = self.client.get(
            reverse("interviews:session", kwargs={"public_id": session.public_id})
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("result", response["Location"])

    def test_finishing_ends_the_session(self):
        session = self.practice_session()
        with fake_ai():
            self.client.post(
                reverse("interviews:finish", kwargs={"public_id": session.public_id}),
                {"action": "abandon"},
            )

        session.refresh_from_db()
        self.assertEqual(session.state, SessionState.ABANDONED)


class ResultScreenTests(ScreenFixture):
    def test_a_practice_result_is_visible_to_its_candidate(self):
        session = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=turn, text="I did it.")
        with fake_ai():
            engine.conclude(session=session)

        evaluation.submit_scorecard(
            session=session, evaluator=self.profile.user, competency="ownership", score=4
        )
        evaluation.conclude(session=session)

        response = self.client.get(
            reverse("interviews:result", kwargs={"public_id": session.public_id})
        )
        # The score and the privacy promise. The hire/no-hire word belongs to an
        # employer's interview, not to somebody's own rehearsal — see
        # `test_a_practice_result_leads_with_what_to_work_on`.
        self.assertContains(response, "4")
        self.assertContains(response, "yours alone")

    def test_a_practice_result_leads_with_what_to_work_on(self):
        """Practice was headlined "No — 2.50 / 5".

        Nobody is hiring: it is a private rehearsal, and a hire/no-hire verdict
        on it is both discouraging and not a thing anybody can act on.
        """
        session = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=turn, text="I did it.")
        with fake_ai():
            engine.conclude(session=session)

        evaluation.submit_scorecard(
            session=session, evaluator=self.profile.user, competency="communication", score=1
        )
        evaluation.submit_scorecard(
            session=session, evaluator=self.profile.user, competency="problem_solving", score=4
        )
        evaluation.conclude(session=session)

        response = self.client.get(
            reverse("interviews:result", kwargs={"public_id": session.public_id})
        )

        self.assertContains(response, "Work on communication")
        # The slug never reaches the candidate.
        self.assertNotContains(response, "problem_solving")
        self.assertContains(response, "problem solving")
        # And the score is still shown: it is why practising is worth anything.
        self.assertContains(response, "/ 5")

    def test_a_result_still_being_read_waits_by_itself(self):
        """The page said "a minute or two" and then did nothing at all.

        Somebody who had just answered eight questions was left reloading to
        find out whether their interview had been read — the exact feeling that
        sentence was written to prevent.
        """
        session = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=turn, text="I did it.")
        with fake_ai():
            engine.conclude(session=session)

        response = self.client.get(
            reverse("interviews:result", kwargs={"public_id": session.public_id})
        )

        self.assertContains(response, "being read")
        self.assertContains(response, 'hx-trigger="every 5s"')
        self.assertContains(response, "updates itself")

    def test_a_finished_result_stops_waiting(self):
        session = self.practice_session()
        with fake_ai():
            turn = engine.next_question(session=session)
        engine.submit_answer(session=session, turn=turn, text="I did it.")
        with fake_ai():
            engine.conclude(session=session)
        evaluation.submit_scorecard(
            session=session, evaluator=self.profile.user, competency="ownership", score=4
        )
        evaluation.conclude(session=session)

        response = self.client.get(
            reverse("interviews:result", kwargs={"public_id": session.public_id})
        )

        self.assertNotContains(response, 'hx-trigger="every 5s"')

    def test_an_unshared_employer_result_says_so_rather_than_showing_nothing(self):
        # "We'll be in touch" with no explanation is what this improves on.
        application = application_for(self.profile, self.job)
        session = InterviewSession.objects.create(
            application=application,
            candidate=self.profile,
            template=self.template,
            kind="ai_text",
            state=SessionState.COMPLETED,
        )
        evaluation.submit_scorecard(
            session=session, evaluator=self.owner, competency="ownership", score=2
        )
        evaluation.conclude(session=session, actor=self.owner)

        response = self.client.get(
            reverse("interviews:result", kwargs={"public_id": session.public_id})
        )
        self.assertContains(response, "has not shared feedback")
        self.assertNotContains(response, "Strong no")

    def test_a_shared_employer_result_is_visible(self):
        application = application_for(self.profile, self.job)
        session = InterviewSession.objects.create(
            application=application,
            candidate=self.profile,
            template=self.template,
            kind="ai_text",
            state=SessionState.COMPLETED,
        )
        evaluation.submit_scorecard(
            session=session, evaluator=self.owner, competency="ownership", score=5
        )
        evaluation.conclude(session=session, actor=self.owner)
        evaluation.share_with_candidate(session=session, actor=self.owner)

        response = self.client.get(
            reverse("interviews:result", kwargs={"public_id": session.public_id})
        )
        self.assertContains(response, "Strong yes")


class PracticeTitleTests(ScreenFixture):
    """MIS-02: practice for a posting is named after the posting."""

    def test_a_job_practice_shows_the_job_title_on_the_result_and_the_hub(self):
        with fake_ai():
            session = engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                job=self.job,
            )
            engine.conclude(session=session)

        result = self.client.get(
            reverse("interviews:result", kwargs={"public_id": session.public_id})
        )
        hub = self.client.get(reverse("interviews:hub"))

        for response in (result, hub):
            self.assertContains(response, self.job.title)


class EmployerScreenTests(ScreenFixture):
    def setUp(self):
        super().setUp()
        self.application = application_for(self.profile, self.job)
        self.session = InterviewSession.objects.create(
            application=self.application,
            candidate=self.profile,
            template=live_template(),
            kind="live",
            state=SessionState.COMPLETED,
            scheduled_at=timezone.now(),
        )
        self.client.force_login(self.owner)
        self.list_url = reverse("employer:interviews", args=[self.company.slug])
        self.detail_url = reverse(
            "employer:interview_detail", args=[self.company.slug, self.session.public_id]
        )

    def test_the_list_renders(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Publish times")

    def test_the_list_is_a_404_when_the_flag_is_off(self):
        disable_interviews(self)
        self.assertEqual(self.client.get(self.list_url).status_code, 404)

    def test_publishing_times_creates_slots(self):
        starts, _ = window(hours_ahead=48)
        response = self.client.post(
            reverse("employer:interview_slots", args=[self.company.slug]),
            {
                "date": starts.date().isoformat(),
                "time": starts.strftime("%H:%M"),
                "minutes": "45",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(scheduling.open_slots(company=self.company).exists())

    def test_a_time_that_has_passed_leaves_the_published_list(self):
        from apps.companies.models import CompanyMembership
        from apps.interviews.models import InterviewSlot

        membership = CompanyMembership.objects.get(company=self.company, user=self.owner)
        ahead, ahead_end = window(hours_ahead=48)
        past = timezone.now() - dt.timedelta(days=2)
        InterviewSlot.objects.create(interviewer=membership, starts_at=ahead, ends_at=ahead_end)
        InterviewSlot.objects.create(
            interviewer=membership, starts_at=past, ends_at=past + dt.timedelta(minutes=45)
        )

        response = self.client.get(self.list_url)

        self.assertEqual([slot.starts_at for slot in response.context["slots"]], [ahead])

    def test_publishing_with_no_date_is_a_message_not_a_crash(self):
        response = self.client.post(
            reverse("employer:interview_slots", args=[self.company.slug]), {"time": "10:00"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(scheduling.open_slots(company=self.company).exists())

    def test_the_detail_screen_shows_the_transcript(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your scorecard")

    def test_submitting_a_scorecard_records_it(self):
        self.client.post(
            self.detail_url,
            {
                "action": "score",
                "competency": "problem_solving",
                "score": "4",
                "rationale": "Good.",
            },
        )
        self.assertTrue(
            InterviewEvaluation.objects.filter(
                session=self.session, evaluator=self.owner, score=4
            ).exists()
        )

    def test_another_interviewers_unsubmitted_scorecard_is_not_rendered(self):
        # The anchoring rule, asserted through the screen.
        other = create_profile().user
        add_member(self.company, other, Role.INTERVIEWER)
        InterviewEvaluation.objects.create(
            session=self.session,
            competency="problem_solving",
            evaluator=other,
            score=5,
            rationale="UNSUBMITTED-RATIONALE-MARKER",
            submitted_at=None,
        )

        response = self.client.get(self.detail_url)
        self.assertNotContains(response, "UNSUBMITTED-RATIONALE-MARKER")

    def test_a_submitted_scorecard_is_rendered_for_everybody(self):
        other = create_profile().user
        add_member(self.company, other, Role.INTERVIEWER)
        evaluation.submit_scorecard(
            session=self.session,
            evaluator=other,
            competency="problem_solving",
            score=5,
            rationale="SUBMITTED-RATIONALE-MARKER",
        )

        response = self.client.get(self.detail_url)
        self.assertContains(response, "SUBMITTED-RATIONALE-MARKER")

    def test_a_practice_session_is_not_reachable_from_the_employer_portal(self):
        # The promise practice mode makes.
        practice = self.practice_session()
        url = reverse("employer:interview_detail", args=[self.company.slug, practice.public_id])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_another_companys_interview_is_a_404(self):
        stranger = create_profile().user
        other_company, _ = company_with_owner(stranger)
        self.client.force_login(stranger)

        url = reverse(
            "employer:interview_detail", args=[other_company.slug, self.session.public_id]
        )
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_an_interview_beyond_the_access_window_is_a_404_for_its_interviewer(self):
        # §11.4 and §2.3: an Interviewer's access runs until thirty days after.
        from apps.accounts.tests.factories import create_user
        from apps.companies.permissions import Role
        from apps.companies.tests.factories import add_member
        from apps.interviews.models import InterviewParticipant

        interviewer = add_member(self.company, create_user(), Role.INTERVIEWER)
        InterviewParticipant.objects.create(session=self.session, user=interviewer.user)
        self.client.force_login(interviewer.user)
        self.assertEqual(self.client.get(self.detail_url).status_code, 200)

        InterviewSession.objects.filter(pk=self.session.pk).update(
            ended_at=timezone.now() - dt.timedelta(days=45)
        )
        self.assertEqual(self.client.get(self.detail_url).status_code, 404)

        # The company's own record does not expire.
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.detail_url).status_code, 200)

    def test_concluding_records_the_outcome(self):
        evaluation.submit_scorecard(
            session=self.session, evaluator=self.owner, competency="problem_solving", score=4
        )
        self.client.post(self.detail_url, {"action": "conclude"})

        self.session.refresh_from_db()
        self.assertIsNotNone(getattr(self.session, "outcome", None))

    def test_sharing_marks_the_outcome_shared(self):
        evaluation.submit_scorecard(
            session=self.session, evaluator=self.owner, competency="problem_solving", score=4
        )
        evaluation.conclude(session=self.session, actor=self.owner)

        self.client.post(self.detail_url, {"action": "share"})

        self.session.refresh_from_db()
        self.assertTrue(self.session.outcome.shared_with_candidate)

    def test_an_unknown_action_is_a_message_not_a_crash(self):
        response = self.client.post(self.detail_url, {"action": "detonate"})
        self.assertEqual(response.status_code, 302)


class BookingScreenTests(ScreenFixture):
    def setUp(self):
        super().setUp()
        self.application = application_for(self.profile, self.job)
        scheduling.publish_slots(
            membership=self.membership, actor=self.owner, windows=[window(hours_ahead=30)]
        )
        self.url = reverse("interviews:book", kwargs={"application_id": self.application.public_id})

    def test_the_screen_lists_open_times(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm this time")

    def test_booking_creates_a_scheduled_session(self):
        slot = scheduling.open_slots(company=self.company).first()
        response = self.client.post(self.url, {"slot": slot.pk})

        self.assertEqual(response.status_code, 302)
        slot.refresh_from_db()
        self.assertEqual(slot.state, "booked")

    def test_another_candidates_application_is_a_404(self):
        stranger = create_profile()
        self.client.force_login(stranger.user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_slot_that_has_gone_is_a_404(self):
        slot = scheduling.open_slots(company=self.company).first()
        scheduling.book(
            slot=slot,
            application=self.application,
            actor=self.owner,
            template=live_template(slug="second-live"),
        )

        response = self.client.post(self.url, {"slot": slot.pk})
        self.assertEqual(response.status_code, 404)


class SchedulingLoopTests(ScreenFixture):
    """Employer publishes, candidate books, either side moves it (FR-10.6, FR-10.7).

    The loop is asserted end to end because it was broken end to end: every
    piece existed as a service and a screen, and `interviews:book` was linked
    from nowhere in the product. An employer opened times, moved somebody to
    the interview stage, and the two of them waited for each other.
    """

    def setUp(self):
        super().setUp()
        self.application = application_for(self.profile, self.job)
        scheduling.publish_slots(
            membership=self.membership,
            actor=self.owner,
            windows=[window(hours_ahead=30), window(hours_ahead=54)],
        )
        self._move_to_interview_stage()

    def _move_to_interview_stage(self) -> None:
        from apps.applications.services import pipeline

        stage = pipeline.stage_by_key(self.company, "interview")
        pipeline.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=stage,
        )
        self.application.refresh_from_db()

    def _detail(self):
        return self.client.get(
            reverse("applications:detail", kwargs={"public_id": self.application.public_id})
        )

    def test_the_application_offers_a_time_once_the_employer_has_opened_some(self):
        self.assertContains(self._detail(), "Pick a time")

    def test_the_application_shows_the_interview_once_it_is_booked(self):
        slot = scheduling.open_slots(company=self.company).first()
        self.client.post(
            reverse("interviews:book", kwargs={"application_id": self.application.public_id}),
            {"slot": slot.pk},
        )

        response = self._detail()
        self.assertContains(response, "Your interview")
        self.assertContains(response, "Move it")

    def test_a_candidate_choosing_again_moves_their_interview_rather_than_booking_a_second(self):
        url = reverse("interviews:book", kwargs={"application_id": self.application.public_id})
        first, second = list(scheduling.open_slots(company=self.company)[:2])

        self.client.post(url, {"slot": first.pk})
        self.client.post(url, {"slot": second.pk})

        live = self.application.interview_sessions.filter(state=SessionState.SCHEDULED)
        self.assertEqual(live.count(), 1)
        self.assertEqual(live.first().scheduled_at, second.starts_at)

    def test_an_employer_can_move_an_interview_rather_than_only_cancel_it(self):
        first, second = list(scheduling.open_slots(company=self.company)[:2])
        session = scheduling.book(
            slot=first,
            application=self.application,
            actor=self.owner,
            template=live_template(slug="live-move"),
        )

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse(
                "employer:interview_detail",
                kwargs={"company_slug": self.company.slug, "public_id": session.public_id},
            ),
            {"action": "reschedule", "slot": second.pk, "reason": "The interviewer is unwell."},
        )

        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertEqual(session.state, SessionState.CANCELLED)
        self.assertEqual(session.cancelled_reason, "The interviewer is unwell.")

        moved = self.application.interview_sessions.get(state=SessionState.SCHEDULED)
        self.assertEqual(moved.rescheduled_from_id, session.pk)
        self.assertEqual(moved.scheduled_at, second.starts_at)

    def test_no_time_is_offered_before_the_interview_stage(self):
        from apps.applications.services import pipeline

        pipeline.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline.stage_by_key(self.company, "reviewing"),
        )

        self.assertNotContains(self._detail(), "Pick a time")
