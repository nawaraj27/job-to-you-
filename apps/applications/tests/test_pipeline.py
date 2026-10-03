"""
The pipeline (FR-6.3, FR-6.4, FR-6.7, FR-6.10) and staged contact revelation
(ADR-A34).

The contact tests are the ones that matter most. "An employer gets a phone
number when they decide to interview, not when somebody applies" is a promise
the product makes on the apply screen, and it is worth several failing cases.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.applications.models import ApplicationEvent, PipelineStage, StageKind
from apps.applications.services import pipeline as pipeline_service
from apps.applications.tests.factories import seed_templates, submit
from apps.audit.models import AuditLog
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotAllowed, NotFound
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import OutboxEvent


class DefaultStageTests(TestCase):
    def test_the_default_pipeline_is_seeded_once(self):
        pipeline_service.ensure_default_stages()
        first = PipelineStage.objects.filter(company__isnull=True).count()

        pipeline_service.ensure_default_stages()
        self.assertEqual(PipelineStage.objects.filter(company__isnull=True).count(), first)

    def test_a_company_with_no_stages_uses_the_platform_default(self):
        company, _ = company_with_owner(create_user())
        stages = pipeline_service.stages_for(company)

        self.assertEqual(stages[0].key, "new")
        self.assertTrue(all(stage.company_id is None for stage in stages))

    def test_the_interview_stage_is_the_one_that_unlocks_contact(self):
        company, _ = company_with_owner(create_user())
        revealing = [s.key for s in pipeline_service.stages_for(company) if s.reveals_contact]
        self.assertEqual(revealing, ["interview"])

    def test_a_pipeline_always_has_somewhere_to_end(self):
        company, _ = company_with_owner(create_user())
        kinds = {stage.kind for stage in pipeline_service.stages_for(company)}
        self.assertIn(StageKind.TERMINAL, kinds)


class MoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())

    def stage(self, key: str) -> PipelineStage:
        return pipeline_service.stage_by_key(self.company, key)

    def move(self, key: str, **kwargs):
        return pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=self.stage(key),
            **kwargs,
        )

    def test_moving_changes_the_stage_and_touches_the_activity_clock(self):
        before = self.application.last_activity_at
        self.move("reviewing")

        self.application.refresh_from_db()
        self.assertEqual(self.application.stage.key, "reviewing")
        self.assertGreater(self.application.last_activity_at, before)

    def test_moving_appends_to_the_one_timeline_and_the_candidate_sees_it(self):
        self.move("reviewing")

        entry = ApplicationEvent.objects.filter(type="application.stage_changed").get()
        self.assertTrue(entry.visible_to_candidate)
        self.assertEqual(entry.from_stage.key, "new")
        self.assertEqual(entry.to_stage.key, "reviewing")

    def test_moving_to_the_same_stage_is_refused(self):
        with self.assertRaises(InvalidState):
            self.move("new")

    def test_a_stage_from_another_company_is_not_found(self):
        other, _ = company_with_owner(create_user(email="other@example.test"))
        foreign = PipelineStage.objects.create(
            company=other, key="theirs", label="Theirs", kind=StageKind.OPEN
        )
        with self.assertRaises(NotFound):
            pipeline_service.move(
                application=self.application,
                actor=self.owner,
                membership=self.membership,
                stage=foreign,
            )

    def test_closing_needs_a_reason(self):
        with self.assertRaises(InvalidState):
            self.move("declined")

    def test_closing_records_the_reason_and_the_time(self):
        self.move("declined", reason="Not enough Django")

        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.closed_at)
        self.assertEqual(self.application.closed_reason, "Not enough Django")
        self.assertFalse(self.application.is_live)

    def test_closing_needs_the_decide_grant(self):
        interviewer = create_user(email="interviewer@example.test")
        membership = add_member(self.company, interviewer, Role.INTERVIEWER)

        with self.assertRaises(NotAllowed):
            pipeline_service.move(
                application=self.application,
                actor=interviewer,
                membership=membership,
                stage=self.stage("declined"),
                reason="No",
            )

        self.application.refresh_from_db()
        self.assertTrue(self.application.is_live)

    def test_reopening_a_closed_application_clears_the_closure(self):
        self.move("declined", reason="Too early")
        self.move("reviewing")

        self.application.refresh_from_db()
        self.assertIsNone(self.application.closed_at)
        self.assertEqual(self.application.closed_reason, "")
        self.assertTrue(self.application.is_live)

    def test_reopening_needs_the_same_grant_that_closing_did(self):
        # A rejection anyone could quietly reverse is not a decision anybody is
        # accountable for.
        self.move("declined", reason="Too early")

        viewer = create_user(email="viewer@example.test")
        membership = add_member(self.company, viewer, Role.VIEWER)

        with self.assertRaises(NotAllowed):
            pipeline_service.move(
                application=self.application,
                actor=viewer,
                membership=membership,
                stage=self.stage("reviewing"),
            )

        self.application.refresh_from_db()
        self.assertFalse(self.application.is_live)

    def test_a_withdrawn_application_cannot_be_moved(self):
        from apps.applications.services import apply as apply_service

        apply_service.withdraw(application=self.application, actor=self.application.candidate.user)
        with self.assertRaises(InvalidState):
            self.move("reviewing")

    def test_a_move_emits_an_event_and_an_audit_row(self):
        self.move("reviewing")
        self.assertTrue(OutboxEvent.objects.filter(type="application.stage_changed").exists())
        self.assertTrue(AuditLog.objects.filter(verb="application.stage_changed").exists())

    def test_closing_emits_its_own_event(self):
        self.move("declined", reason="Not this time")
        self.assertTrue(OutboxEvent.objects.filter(type="application.closed").exists())


class ContactRevelationTests(TestCase):
    """ADR-A34. The promise the apply screen makes."""

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())

    def move(self, key: str, **kwargs):
        return pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, key),
            **kwargs,
        )

    def test_contact_stays_locked_through_the_early_stages(self):
        self.move("reviewing")
        self.move("shortlisted")

        self.application.refresh_from_db()
        self.assertIsNone(self.application.contact_revealed_at)

    def test_reaching_the_interview_stage_unlocks_contact(self):
        self.move("interview")

        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.contact_revealed_at)

    def test_unlocking_tells_the_candidate(self):
        self.move("interview")

        entry = ApplicationEvent.objects.filter(type="application.contact_revealed").get()
        self.assertTrue(entry.visible_to_candidate)

    def test_unlocking_is_audited_and_emitted(self):
        self.move("interview")
        self.assertTrue(AuditLog.objects.filter(verb="application.contact_revealed").exists())
        self.assertTrue(OutboxEvent.objects.filter(type="application.contact_revealed").exists())

    def test_manual_unlocking_needs_a_reason(self):
        with self.assertRaises(InvalidState):
            pipeline_service.reveal_contact(
                application=self.application,
                actor=self.owner,
                membership=self.membership,
                basis="manual",
            )

    def test_manual_unlocking_with_a_reason_works_and_records_it(self):
        pipeline_service.reveal_contact(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            basis="manual",
            reason="Arranging a call",
        )

        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.contact_revealed_at)
        entry = ApplicationEvent.objects.filter(type="application.contact_revealed").get()
        self.assertEqual(entry.payload["reason"], "Arranging a call")

    def test_unlocking_twice_does_not_move_the_date(self):
        self.move("interview")
        self.application.refresh_from_db()
        first = self.application.contact_revealed_at

        pipeline_service.reveal_contact(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            basis="manual",
            reason="again",
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.contact_revealed_at, first)


class BulkMoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.applications = [submit(job=self.job, profile=create_profile()) for _ in range(3)]

    def test_a_bulk_move_moves_them_all(self):
        result = pipeline_service.move_many(
            applications=self.applications,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "reviewing"),
        )
        self.assertEqual(result, {"moved": 3, "skipped": 0, "refused": 0, "reasons": []})

    def test_one_refusal_does_not_take_the_batch_with_it(self):
        pipeline_service.move(
            application=self.applications[0],
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "reviewing"),
        )

        result = pipeline_service.move_many(
            applications=self.applications,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "reviewing"),
        )
        # The one already there is skipped; the other two still move.
        self.assertEqual(result, {"moved": 2, "skipped": 1, "refused": 0, "reasons": []})


class ConfigureStagesTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)

    def configure(self, specs):
        return pipeline_service.configure_stages(
            company=self.company, actor=self.owner, specs=specs
        )

    def test_a_company_can_define_its_own_pipeline(self):
        stages = self.configure(
            [
                {"key": "inbox", "label": "Inbox", "kind": StageKind.OPEN},
                {"key": "done", "label": "Done", "kind": StageKind.TERMINAL},
            ]
        )
        self.assertEqual([stage.key for stage in stages], ["inbox", "done"])
        self.assertEqual(
            [stage.key for stage in pipeline_service.stages_for(self.company)],
            ["inbox", "done"],
        )

    def test_a_pipeline_with_nowhere_to_end_is_refused(self):
        with self.assertRaises(InvalidState):
            self.configure([{"key": "inbox", "label": "Inbox", "kind": StageKind.OPEN}])

    def test_an_empty_pipeline_is_refused(self):
        with self.assertRaises(InvalidState):
            self.configure([])

    def test_an_invented_kind_is_refused(self):
        with self.assertRaises(InvalidState):
            self.configure([{"key": "x", "label": "X", "kind": "vibes"}])

    def test_a_stage_still_carrying_applications_cannot_be_dropped(self):
        seed_templates()
        self.configure(
            [
                {"key": "inbox", "label": "Inbox", "kind": StageKind.OPEN},
                {"key": "done", "label": "Done", "kind": StageKind.TERMINAL},
            ]
        )
        job = publish(create_job(self.company))
        submit(job=job, profile=create_profile())

        with self.assertRaises(InvalidState):
            self.configure([{"key": "done", "label": "Done", "kind": StageKind.TERMINAL}])

    def test_a_private_stage_shows_the_candidate_something_else(self):
        # FR-6.10: an internal stage name the company marked private is never
        # what the candidate reads.
        stages = self.configure(
            [
                {
                    "key": "maybe",
                    "label": "Probably not",
                    "kind": StageKind.OPEN,
                    "is_visible_to_candidate": False,
                    "candidate_label": "Under review",
                },
                {"key": "done", "label": "Done", "kind": StageKind.TERMINAL},
            ]
        )
        self.assertEqual(stages[0].public_label, "Under review")

    def test_configuring_is_audited(self):
        self.configure([{"key": "done", "label": "Done", "kind": StageKind.TERMINAL}])
        self.assertTrue(AuditLog.objects.filter(verb="pipeline.configured").exists())
