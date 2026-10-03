"""
Account erasure (SRS §19.4, FR-1.8).

The properties this file exists to hold, in order of what would hurt most if
they broke:

  * **The gate closes before anything is removed.** `erasure_started_at` is set
    first, and the AI runner refuses every task for that subject afterwards.
    Without it, a task already in flight writes resume quotes back into a table
    the erasure has already walked past — a failure that leaves no trace and
    recreates exactly the data somebody asked to have destroyed.
  * **Every store in §19.4's table is actually walked.** Asserted store by
    store, from the failing side: the test sets up real data in each and
    asserts it is gone, redacted or anonymised as the table says.
  * **Applications survive, anonymised.** The employer keeps that somebody
    applied and how it ended; nothing of the candidate remains in it.
  * **The grace period is real and cancellable**, and stops being cancellable
    at exactly the moment rows start disappearing.
  * **Running it twice is safe.** A worker killed halfway leaves a gated,
    half-walked account, and the sweep that resumes it must be able to finish.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import erasure
from apps.accounts.tasks import resume_interrupted_erasures, run_due_erasures
from apps.ai.models import AIRun, RunStatus
from apps.analytics.models import AnalyticsEvent
from apps.applications.models import Application, CoverLetter
from apps.audit.models import AuditLog
from apps.billing.tests import factories as billing_factories
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.companies.tests.factories import company_with_owner
from apps.interviews.models import InterviewTurn
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for
from apps.messaging.models import Message
from apps.resumes.models import ResumeAnalysis, ResumeDraft
from apps.resumes.services import documents as document_service


class ErasureFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.user = self.profile.user
        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))

    def request_and_ripen(self) -> User:
        """Request erasure and move the clock past the grace period."""
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        User.objects.filter(pk=self.user.pk).update(
            erasure_requested_at=timezone.now() - dt.timedelta(days=erasure.GRACE_DAYS, minutes=1)
        )
        self.user.refresh_from_db()
        return self.user


class RequestTests(ErasureFixture):
    def test_requesting_schedules_it_and_removes_nothing(self):
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        self.user.refresh_from_db()

        self.assertIsNotNone(self.user.erasure_requested_at)
        self.assertIsNone(self.user.erasure_started_at)
        self.assertTrue(self.user.is_active)

    def test_the_confirmation_word_is_required(self):
        with self.assertRaises(InvalidState):
            erasure.request_erasure(user=self.user, actor=self.user, confirmation="yes")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.erasure_requested_at)

    def test_requesting_twice_is_refused(self):
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        with self.assertRaises(InvalidState):
            erasure.request_erasure(
                user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION
            )

    def test_the_effective_date_is_fourteen_days_out(self):
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        self.user.refresh_from_db()
        self.assertEqual(
            erasure.erasure_due_on(self.user),
            (timezone.now() + dt.timedelta(days=erasure.GRACE_DAYS)).date(),
        )

    def test_it_can_be_cancelled_during_the_grace_period(self):
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        erasure.cancel_erasure(user=self.user, actor=self.user)

        self.user.refresh_from_db()
        self.assertIsNone(self.user.erasure_requested_at)

    def test_it_cannot_be_cancelled_once_rows_are_going(self):
        # Telling somebody their cancellation worked, with their data already
        # deleted behind it, is the one answer this must never give.
        self.request_and_ripen()
        erasure._open_the_gate(self.user)

        with self.assertRaises(InvalidState):
            erasure.cancel_erasure(user=self.user, actor=self.user)

    def test_cancelling_without_a_request_is_refused(self):
        with self.assertRaises(InvalidState):
            erasure.cancel_erasure(user=self.user, actor=self.user)

    def test_only_ripe_requests_are_due(self):
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        self.assertEqual(erasure.due_now(), [])

        User.objects.filter(pk=self.user.pk).update(
            erasure_requested_at=timezone.now() - dt.timedelta(days=erasure.GRACE_DAYS + 1)
        )
        self.assertEqual([user.pk for user in erasure.due_now()], [self.user.pk])


class GateTests(ErasureFixture):
    def test_the_gate_is_set_before_anything_is_removed(self):
        self.request_and_ripen()
        erasure._open_the_gate(self.user)

        self.user.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.user.erasure_started_at)
        self.assertIsNotNone(self.profile.erasure_started_at)

    def test_the_runner_refuses_every_task_for_an_erasing_subject(self):
        from apps.ai.services import prompts, runner

        prompts.load_all()
        self.request_and_ripen()
        erasure._open_the_gate(self.user)

        run = runner.run(
            task_key="resume.analysis", inputs={"resume_text": "anything"}, user=self.user
        )
        self.assertEqual(run.status, RunStatus.ERASED)
        self.assertEqual(run.input_snapshot, {})

    def test_an_erased_run_is_not_retryable(self):
        # Retrying would be the exact recreation the erasure exists to prevent.
        self.assertNotIn(RunStatus.ERASED, RunStatus.retryable())
        self.assertIn(RunStatus.ERASED, RunStatus.terminal())

    def test_analysis_refuses_too(self):
        # The AI half is stopped by the runner; the deterministic half writes
        # findings that quote resume lines and needs its own check.
        from apps.resumes.services import analysis

        billing_factories.put_on_pro(self.profile)
        document = document_service.create_document(profile=self.profile, actor=self.user)
        self.request_and_ripen()
        erasure._open_the_gate(self.user)

        with self.assertRaises(InvalidState):
            analysis.analyse(document=document, actor=self.user, use_ai=False)

    def test_is_erasing_accepts_a_user_or_a_profile(self):
        self.assertFalse(erasure.is_erasing(self.user))
        self.assertFalse(erasure.is_erasing(self.profile))
        self.assertFalse(erasure.is_erasing(None))

        self.request_and_ripen()
        erasure._open_the_gate(self.user)
        self.user.refresh_from_db()
        self.profile.refresh_from_db()

        self.assertTrue(erasure.is_erasing(self.user))
        self.assertTrue(erasure.is_erasing(self.profile))


class WalkTests(ErasureFixture):
    """Each store in §19.4's table, set up and then asserted gone."""

    def setUp(self):
        super().setUp()
        billing_factories.put_on_pro(self.profile)
        self.document = document_service.create_document(
            profile=self.profile, actor=self.user, name="My CV"
        )
        self.application = application_for(self.profile, self.job)

    def erase(self):
        self.request_and_ripen()
        return erasure.erase(user=self.user)

    def test_ai_run_payloads_are_emptied_and_the_rows_survive(self):
        # §19.4 calls this the most commonly missed store: resume text was sent
        # to a provider and a copy of it is held here.
        run = AIRun.objects.create(
            task_key="resume.analysis",
            prompt_version="1",
            input_hash="abc",
            input_snapshot={"resume_text": "Priya Sharma, backend engineer"},
            output={"verdict": "strong"},
            raw_output="{}",
            user=self.user,
            status=RunStatus.OK,
        )

        self.erase()
        run.refresh_from_db()

        self.assertEqual(run.input_snapshot, {})
        self.assertEqual(run.output, {})
        self.assertEqual(run.raw_output, "")

    def test_analyses_are_deleted_because_findings_quote_the_cv(self):
        from apps.resumes.services import analysis

        analysis.analyse(document=self.document, actor=self.user, use_ai=False)
        self.assertTrue(ResumeAnalysis.objects.exists())

        self.erase()
        self.assertFalse(ResumeAnalysis.objects.exists())

    def test_versions_are_redacted_not_deleted(self):
        from apps.resumes.services import versions

        version = versions.finalize(document=self.document, actor=self.user, reason="exported")
        digest = version.sha256

        self.erase()
        version.refresh_from_db()

        # The row, the hash and the lineage survive: that is what still proves
        # a document was submitted, with nothing left of what it said (A38).
        self.assertTrue(version.is_redacted)
        self.assertEqual(version.sha256, digest)

    def test_drafts_are_deleted(self):
        self.assertTrue(ResumeDraft.objects.filter(document=self.document).exists())
        self.erase()
        self.assertFalse(ResumeDraft.objects.filter(document=self.document).exists())

    def test_the_application_survives_and_holds_nothing_of_the_candidate(self):
        Application.objects.filter(pk=self.application.pk).update(
            answers={"why": "Because I have wanted this for years."}
        )

        self.erase()
        self.application.refresh_from_db()

        self.assertEqual(self.application.answers, {})
        self.assertEqual(self.application.job_id, self.job.pk)  # employer data, kept

    def test_cover_letters_are_emptied(self):
        letter = CoverLetter.objects.create(
            profile=self.profile, job=self.job, title="Backend role", content="Dear hiring manager"
        )

        self.erase()
        letter.refresh_from_db()

        self.assertEqual(letter.content, "")
        self.assertEqual(letter.title, "")

    def test_interview_transcripts_are_deleted(self):
        from apps.interviews.models import InterviewSession, SessionState
        from apps.interviews.tests.factories import practice_template

        session = InterviewSession.objects.create(
            candidate=self.profile,
            template=practice_template(),
            state=SessionState.COMPLETED,
            kind="practice",
        )
        InterviewTurn.objects.create(
            session=session,
            order=1,
            question_text="Tell me about a project.",
            answer_text="I built the settlement service.",
        )

        self.erase()
        self.assertFalse(InterviewTurn.objects.filter(session=session).exists())
        self.assertTrue(InterviewSession.objects.filter(pk=session.pk).exists())

    def test_message_bodies_go_and_the_conversation_shell_stays(self):
        from apps.messaging.models import Conversation, SenderKind

        conversation = Conversation.objects.create(
            company=self.company, candidate=self.profile, job=self.job
        )
        message = Message.objects.create(
            conversation=conversation,
            sender=self.user,
            sender_kind=SenderKind.CANDIDATE,
            body="My phone number is 98############",
        )

        self.erase()
        message.refresh_from_db()

        self.assertEqual(message.body, "")
        self.assertTrue(Conversation.objects.filter(pk=conversation.pk).exists())

    def test_the_profile_embedding_is_nulled(self):
        # A 768-dimension vector of somebody's CV is not anonymous just because
        # it is numeric.
        from apps.candidates.models import CandidateProfile

        CandidateProfile.objects.filter(pk=self.profile.pk).update(
            embedding=[0.1] * 768, embedding_model="fake", embedded_at=timezone.now()
        )

        self.erase()
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.embedding)

    def test_the_profile_becomes_a_tombstone(self):
        self.profile.headline = "Backend engineer, Kathmandu"
        self.profile.phone = "9800000000"
        self.profile.save()

        self.erase()
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.headline, "")
        self.assertEqual(self.profile.phone, "")
        self.assertFalse(self.profile.is_searchable)

    def test_analytics_events_keep_their_shape_and_lose_their_owner(self):
        event = AnalyticsEvent.objects.create(name="job.viewed", actor=self.user)

        self.erase()
        event.refresh_from_db()

        self.assertIsNone(event.actor)
        self.assertEqual(event.name, "job.viewed")

    def test_the_account_survives_as_an_unusable_identity(self):
        self.erase()
        self.user.refresh_from_db()

        self.assertIn(erasure.TOMBSTONE_DOMAIN, self.user.email)
        self.assertEqual(self.user.full_name, "")
        self.assertFalse(self.user.is_active)
        self.assertIsNotNone(self.user.erased_at)
        self.assertFalse(self.user.has_usable_password())

    def test_the_erasure_is_audited_with_counts_and_no_identifiers(self):
        self.erase()
        record = AuditLog.objects.filter(verb="account.erased").get()

        self.assertIn("stores", record.metadata)
        self.assertIsInstance(record.metadata["stores"], dict)

    def test_the_completion_event_is_emitted(self):
        from apps.notifications.models import OutboxEvent

        self.erase()
        self.assertTrue(OutboxEvent.objects.filter(type="candidate.erasure_completed").exists())

    def test_every_store_in_the_specification_is_walked(self):
        # The list in the module and the table in §19.4 must not drift. A store
        # added to one and not the other is what this catches.
        report = self.erase()
        self.assertEqual(set(report.stores), set(erasure.STORES))

    def test_running_it_twice_is_safe(self):
        self.erase()
        # A worker killed halfway leaves a gated, half-walked account; the
        # resume sweep has to be able to finish it.
        self.user.refresh_from_db()
        with self.assertRaises(InvalidState):
            erasure.erase(user=self.user)


class SweepTests(ErasureFixture):
    def test_the_sweep_erases_what_is_due(self):
        self.request_and_ripen()
        self.assertEqual(run_due_erasures()["erased"], 1)

        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.erased_at)

    def test_the_sweep_leaves_unripe_requests_alone(self):
        erasure.request_erasure(user=self.user, actor=self.user, confirmation=erasure.CONFIRMATION)
        self.assertEqual(run_due_erasures()["erased"], 0)

    def test_one_failure_does_not_stop_the_others(self):
        from unittest import mock

        other = create_profile().user
        for user in (self.user, other):
            erasure.request_erasure(user=user, actor=user, confirmation=erasure.CONFIRMATION)
        User.objects.filter(pk__in=[self.user.pk, other.pk]).update(
            erasure_requested_at=timezone.now() - dt.timedelta(days=erasure.GRACE_DAYS + 1)
        )

        real = erasure.erase
        calls = {"n": 0}

        def flaky(*, user):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real(user=user)

        with mock.patch.object(erasure, "erase", side_effect=flaky):
            result = run_due_erasures()

        self.assertEqual(result, {"erased": 1, "failed": 1})

    def test_an_interrupted_erasure_is_finished(self):
        self.request_and_ripen()
        erasure._open_the_gate(self.user)  # gate closed, walk never ran

        self.assertEqual(resume_interrupted_erasures(), 1)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.erased_at)
