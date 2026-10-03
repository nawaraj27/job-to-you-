"""
Applying (SRS §5.8, FR-6.1, FR-6.2).

The apply path freezes three things, and each gets a test that would fail if
the freeze were removed: the CV version and the hash of the file that went, the
posting as it read that day, and — by its absence — the match score, which
belongs to P13 and must not be invented here.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.applications.models import (
    Application,
    ApplicationEvent,
    CandidateVisibilityGrant,
    GrantBasis,
)
from apps.applications.services import apply as apply_service
from apps.applications.services import pipeline as pipeline_service
from apps.applications.tests.factories import create_version, seed_templates, submit
from apps.audit.models import AuditLog
from apps.candidates.tests.factories import create_profile
from apps.common.errors import Conflict, InvalidState, NotAllowed
from apps.companies.tests.factories import company_with_owner
from apps.jobs.services import postings
from apps.jobs.tests.factories import create_job, publish
from apps.matching import scoring as matching_scoring
from apps.notifications.models import OutboxEvent


class ApplyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.company, _ = company_with_owner(create_user_owner())
        self.job = publish(create_job(self.company, title="Backend Engineer"))
        self.profile = create_profile()

    def test_applying_creates_a_live_application_at_the_first_stage(self):
        application = submit(job=self.job, profile=self.profile)

        self.assertTrue(application.is_live)
        self.assertEqual(application.stage.key, "new")
        self.assertEqual(application.candidate, self.profile)

    def test_the_cv_is_frozen_by_version_and_by_file_hash(self):
        application = submit(job=self.job, profile=self.profile)

        # ADR-A8: "the CV I sent" has to mean one thing months later.
        self.assertIsNotNone(application.resume_version_id)
        self.assertEqual(len(application.submitted_render_sha256), 64)

    def test_the_posting_is_snapshotted_by_value(self):
        application = submit(job=self.job, profile=self.profile)

        self.assertEqual(application.job_snapshot["title"], "Backend Engineer")
        self.assertEqual(application.job_snapshot["company_name"], self.company.name)

    def test_editing_the_posting_afterwards_does_not_change_the_snapshot(self):
        application = submit(job=self.job, profile=self.profile)
        owner = self.company.memberships.first().user
        postings.update_job(job=self.job, actor=owner, title="Completely Different Role")

        application.refresh_from_db()
        # The whole reason ADR-A23 could delete `JobRevision`.
        self.assertEqual(application.job_snapshot["title"], "Backend Engineer")

    def test_the_match_score_is_frozen_at_submission(self):
        # P13 fills what P8 left empty (FR-8.5). The score is written once and
        # never recomputed: six months on, the candidate has different skills
        # and the scorer has been retuned, and recomputing would silently
        # rewrite what the employer saw when they decided.
        application = submit(job=self.job, profile=self.profile)

        snapshot = application.match_snapshot
        self.assertIn("score", snapshot)
        self.assertIn("band", snapshot)
        self.assertEqual(snapshot["algorithm_version"], matching_scoring.ALGORITHM_VERSION)

    def test_the_frozen_score_does_not_move_when_the_profile_does(self):
        application = submit(job=self.job, profile=self.profile)
        before = dict(application.match_snapshot)

        self.profile.headline = "Now with a much better headline"
        self.profile.save(update_fields=["headline", "updated_at"])

        application.refresh_from_db()
        self.assertEqual(application.match_snapshot, before)

    def test_applying_opens_the_timeline_with_a_shared_entry(self):
        application = submit(job=self.job, profile=self.profile)

        entry = application.timeline.get()
        self.assertEqual(entry.type, "application.submitted")
        self.assertTrue(entry.visible_to_candidate)

    def test_applying_grants_the_company_visibility(self):
        application = submit(job=self.job, profile=self.profile)

        grant = CandidateVisibilityGrant.objects.live().get(
            candidate=self.profile, company=self.company
        )
        self.assertEqual(grant.basis, GrantBasis.APPLICATION)
        self.assertEqual(grant.application_id, application.pk)

    def test_applying_emits_an_event_and_writes_an_audit_row(self):
        submit(job=self.job, profile=self.profile)

        self.assertTrue(OutboxEvent.objects.filter(type="application.submitted").exists())
        self.assertTrue(AuditLog.objects.filter(verb="application.submitted").exists())

    def test_applying_increments_the_posting_counter(self):
        submit(job=self.job, profile=self.profile)
        self.job.refresh_from_db()
        self.assertEqual(self.job.applications_count, 1)

    def test_contact_details_are_not_revealed_by_applying(self):
        application = submit(job=self.job, profile=self.profile)
        # ADR-A34: an application is not consent to be phoned.
        self.assertIsNone(application.contact_revealed_at)


class ApplyGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.company, _ = company_with_owner(create_user_owner())
        self.job = publish(create_job(self.company))
        self.profile = create_profile()

    def test_a_second_live_application_is_refused(self):
        submit(job=self.job, profile=self.profile)
        with self.assertRaises(Conflict):
            submit(job=self.job, profile=self.profile)

    def test_an_unpublished_posting_cannot_be_applied_to(self):
        draft = create_job(self.company)
        with self.assertRaises(InvalidState):
            submit(job=draft, profile=self.profile)

    def test_an_externally_applied_posting_is_refused_here(self):
        external = publish(
            create_job(self.company, apply_mode="external", external_url="https://x.test/jobs/1")
        )
        with self.assertRaises(InvalidState):
            submit(job=external, profile=self.profile)

    def test_somebody_elses_cv_cannot_be_sent(self):
        other = create_version(create_profile())
        with self.assertRaises(NotAllowed):
            submit(job=self.job, profile=self.profile, resume_version=other)

    def test_a_blocked_company_cannot_be_applied_to(self):
        from apps.candidates.models import BlockedCompany

        BlockedCompany.objects.create(profile=self.profile, company=self.company)
        with self.assertRaises(InvalidState):
            submit(job=self.job, profile=self.profile)

    def test_reapplying_inside_the_cooldown_is_refused(self):
        job = publish(create_job(self.company, application_cooldown_days=30))
        application = submit(job=job, profile=self.profile)
        _close(application)

        with self.assertRaises(InvalidState):
            submit(job=job, profile=self.profile)

    def test_reapplying_after_the_cooldown_is_allowed(self):
        job = publish(create_job(self.company, application_cooldown_days=7))
        application = submit(job=job, profile=self.profile)
        _close(application, when=timezone.now() - dt.timedelta(days=30))

        self.assertIsNotNone(submit(job=job, profile=self.profile))

    def test_reapplying_with_no_cooldown_is_allowed_immediately(self):
        application = submit(job=self.job, profile=self.profile)
        _close(application)
        self.assertIsNotNone(submit(job=self.job, profile=self.profile))


class ScreeningAnswerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.company, self.membership = company_with_owner(create_user_owner())
        self.job = create_job(self.company)
        self.profile = create_profile()

    def _publish_with_question(self, **kwargs):
        owner = self.membership.user
        question = postings.add_question(job=self.job, actor=owner, **kwargs)
        publish(self.job)
        return question

    def test_a_required_question_must_be_answered(self):
        self._publish_with_question(text="Notice period?", is_required=True)
        with self.assertRaises(InvalidState):
            submit(job=self.job, profile=self.profile, answers={})

    def test_an_answered_question_is_stored_against_its_id(self):
        question = self._publish_with_question(text="Notice period?", is_required=True)
        application = submit(
            job=self.job, profile=self.profile, answers={str(question.pk): "One month"}
        )
        self.assertEqual(application.answers[str(question.pk)], "One month")

    def test_an_answer_to_a_question_that_is_not_on_the_posting_is_refused(self):
        self._publish_with_question(text="Notice period?", is_required=False)
        from apps.common.errors import NotFound

        with self.assertRaises(NotFound):
            submit(job=self.job, profile=self.profile, answers={"999999": "hello"})

    def test_an_optional_question_may_be_skipped(self):
        self._publish_with_question(text="Anything else?", is_required=False)
        application = submit(job=self.job, profile=self.profile, answers={})
        self.assertEqual(application.answers, {})


class WithdrawTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.company, _ = company_with_owner(create_user_owner())
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        self.application = submit(job=self.job, profile=self.profile)

    def test_withdrawing_ends_the_application(self):
        apply_service.withdraw(application=self.application, actor=self.profile.user)

        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.withdrawn_at)
        self.assertFalse(self.application.is_live)

    def test_withdrawing_twice_is_refused(self):
        apply_service.withdraw(application=self.application, actor=self.profile.user)
        with self.assertRaises(InvalidState):
            apply_service.withdraw(application=self.application, actor=self.profile.user)

    def test_withdrawing_is_never_silent(self):
        apply_service.withdraw(application=self.application, actor=self.profile.user)

        entry = self.application.timeline.filter(type="application.withdrawn").get()
        self.assertTrue(entry.visible_to_candidate)
        self.assertTrue(OutboxEvent.objects.filter(type="application.withdrawn").exists())

    def test_withdrawing_frees_the_slot_for_a_new_application(self):
        apply_service.withdraw(application=self.application, actor=self.profile.user)
        self.assertIsNotNone(submit(job=self.job, profile=self.profile))

    def test_an_accepted_offer_cannot_be_withdrawn_from(self):
        from apps.applications.models import OfferState
        from apps.applications.services import offers as offer_service

        membership = self.company.memberships.first()
        offer = offer_service.draft_offer(
            application=self.application,
            actor=membership.user,
            membership=membership,
            salary="100000",
            currency="NPR",
        )
        offer_service.send(offer=offer, actor=membership.user, membership=membership)
        offer_service.respond(offer=offer, actor=self.profile.user, accept=True)

        self.assertEqual(offer.state, OfferState.ACCEPTED)
        with self.assertRaises(InvalidState):
            apply_service.withdraw(application=self.application, actor=self.profile.user)


class ConstraintTests(TestCase):
    """What survives a bug in the service layer."""

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def test_the_database_refuses_a_second_live_application(self):
        from django.db import IntegrityError, transaction

        company, _ = company_with_owner(create_user_owner())
        job = publish(create_job(company))
        profile = create_profile()
        first = submit(job=job, profile=profile)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Application.objects.create(
                job=job,
                candidate=profile,
                resume_version=first.resume_version,
                stage=pipeline_service.default_stage_for(job),
            )


def create_user_owner():
    from apps.accounts.tests.factories import create_user

    return create_user()


def _close(application: Application, *, when=None) -> None:
    """Close an application directly. Used where closure is setup, not the test."""
    from apps.applications.models import StageKind

    terminal = next(
        stage
        for stage in pipeline_service.stages_for(application.job.company)
        if stage.kind == StageKind.TERMINAL
    )
    Application.objects.filter(pk=application.pk).update(
        stage=terminal, closed_at=when or timezone.now(), closed_reason="Not proceeding"
    )
    ApplicationEvent.objects.create(
        application=application, actor_kind="employer", type="application.stage_changed"
    )


class RateLimitOrderingTests(TestCase):
    """Regression: a refused application must not cost a slot (FR-6.13).

    The limiter is a counter in Redis and a rolled-back transaction does not
    give the count back, so everything that can refuse has to run before the
    limit is consumed.
    """

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.company, self.membership = company_with_owner(create_user_owner())
        self.job = create_job(self.company)
        self.profile = create_profile()
        postings.add_question(
            job=self.job,
            actor=self.membership.user,
            text="Notice period?",
            is_required=True,
        )
        publish(self.job)

    def _remaining(self) -> int:
        from apps.common import ratelimit

        return ratelimit.check(
            ratelimit.APPLICATION_PER_USER,
            f"user:{self.profile.user.pk}",
            consume=False,
        )

    def test_a_refused_application_does_not_consume_the_budget(self):
        before = self._remaining()

        with self.assertRaises(InvalidState):
            submit(job=self.job, profile=self.profile, answers={})

        self.assertEqual(self._remaining(), before)

    def test_a_successful_application_does_consume_it(self):
        before = self._remaining()
        question = self.job.questions.get()

        submit(job=self.job, profile=self.profile, answers={str(question.pk): "One month"})

        self.assertEqual(self._remaining(), before - 1)
