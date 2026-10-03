"""
Retention, and the schedule that keeps it (SRS §15.3, NFR-8, ADR-A12).

Three properties, and the third is the one that would have caught a defect that
shipped twice already:

  * **A sweep removes what is past its window and nothing else.** Each is
    asserted from both sides, because a retention job that deleted too much
    would be indistinguishable from one working correctly until somebody went
    looking for a record that should still exist.
  * **Soft delete has a purge.** ADR-A12's whole justification for `deleted_at`
    is that a purge follows it; without one, "we deleted it" is false.
  * **Every scheduled task exists, and every retention rule is scheduled.**
    P17's rollups and P18's billing cron were both written and never added to
    the beat schedule — a dashboard whose numbers nothing computed, and a
    subscription lifecycle nothing advanced. Neither failed a test, because no
    test looked at the schedule.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.common import retention
from apps.common.tasks import PURGEABLE, purge_soft_deleted
from config.celery import app as celery_app


class ScheduleTests(TestCase):
    """The beat schedule against the code it names."""

    def setUp(self):
        celery_app.loader.import_default_modules()

    def test_every_scheduled_task_exists(self):
        # A typo in a task path is a job that silently never runs.
        missing = [
            entry["task"]
            for entry in celery_app.conf.beat_schedule.values()
            if entry["task"] not in celery_app.tasks
        ]
        self.assertEqual(missing, [])

    def test_every_retention_rule_has_a_scheduled_job(self):
        # §15.3 states a duration per class and NFR-8 says a job executes it.
        # A rule with no job is a promise on a privacy page and nothing else.
        scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
        unscheduled = [rule.key for rule in retention.SCHEDULE if rule.task not in scheduled]
        self.assertEqual(unscheduled, [])

    def test_the_analytics_rollups_are_scheduled(self):
        # Built in P17, unscheduled until P19: the dashboard read rollups that
        # nothing computed.
        scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
        self.assertIn("apps.analytics.tasks.run_rollups", scheduled)
        self.assertIn("apps.analytics.tasks.measure_steering", scheduled)

    def test_the_billing_lifecycle_is_scheduled(self):
        # Built in P18, unscheduled until P19: nothing renewed, nothing chased,
        # nothing ended.
        scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
        for task in (
            "apps.billing.tasks.renew_due",
            "apps.billing.tasks.chase_overdue",
            "apps.billing.tasks.end_cancelled",
            "apps.billing.tasks.end_unpaid",
        ):
            self.assertIn(task, scheduled)

    def test_the_erasure_sweep_is_scheduled(self):
        scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
        self.assertIn("apps.accounts.tasks.run_due_erasures", scheduled)

    def test_a_rule_names_a_real_duration(self):
        for rule in retention.SCHEDULE:
            self.assertGreater(rule.days, 0, rule.key)

    def test_looking_up_an_unknown_rule_is_loud(self):
        with self.assertRaises(KeyError):
            retention.rule("forever")


class SoftDeletePurgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        from apps.candidates.tests.factories import create_profile
        from apps.resumes.services import documents

        self.profile = create_profile()
        self.document = documents.create_document(profile=self.profile, actor=self.profile.user)

    def soft_delete(self, *, days_ago: int) -> None:
        from apps.resumes.models import ResumeDocument

        ResumeDocument.objects.filter(pk=self.document.pk).update(
            deleted_at=timezone.now() - dt.timedelta(days=days_ago)
        )

    def test_a_row_past_the_window_is_purged(self):
        from apps.resumes.models import ResumeDocument

        self.soft_delete(days_ago=retention.SOFT_DELETED_DAYS + 1)
        removed = purge_soft_deleted()

        self.assertEqual(removed.get("resumes.ResumeDocument"), 1)
        self.assertFalse(ResumeDocument.objects.filter(pk=self.document.pk).exists())

    def test_a_recently_deleted_row_is_kept(self):
        # A candidate who deletes a CV by accident on Tuesday should be able to
        # ask for it back on Wednesday.
        from apps.resumes.models import ResumeDocument

        self.soft_delete(days_ago=1)
        purge_soft_deleted()
        self.assertTrue(ResumeDocument.objects.filter(pk=self.document.pk).exists())

    def test_a_live_row_is_never_touched(self):
        from apps.resumes.models import ResumeDocument

        purge_soft_deleted()
        self.assertTrue(ResumeDocument.objects.filter(pk=self.document.pk).exists())

    def test_every_purgeable_model_exists_and_carries_deleted_at(self):
        # The list is written by hand precisely so it can be checked.
        from django.apps import apps as django_apps

        for label, _reason in PURGEABLE:
            model = django_apps.get_model(label)
            self.assertIn("deleted_at", [field.name for field in model._meta.get_fields()], label)

    def test_the_purgeable_list_covers_every_soft_deleted_model(self):
        """Anything with `deleted_at` and no purge grows forever (§5.16 rule 5)."""
        from django.apps import apps as django_apps

        listed = {label for label, _ in PURGEABLE}
        # `Attachment` purges through `files.services.purge`, which also removes
        # the bytes from the bucket — a `.delete()` here would leave the object.
        listed.add("files.Attachment")

        soft_deleted = {
            f"{model._meta.app_label}.{model.__name__}"
            for model in django_apps.get_models()
            if any(field.name == "deleted_at" for field in model._meta.get_fields())
        }
        self.assertEqual(soft_deleted - listed, set())


class TranscriptRetentionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        from apps.candidates.tests.factories import create_profile
        from apps.interviews.models import InterviewSession, InterviewTurn, SessionState
        from apps.interviews.tests.factories import practice_template

        self.profile = create_profile()
        self.session = InterviewSession.objects.create(
            candidate=self.profile,
            template=practice_template(),
            state=SessionState.COMPLETED,
            kind="practice",
            ended_at=timezone.now(),
        )
        InterviewTurn.objects.create(
            session=self.session,
            order=1,
            question_text="Tell me about a project.",
            answer_text="I built the settlement service.",
        )

    def age(self, *, days: int) -> None:
        from apps.interviews.models import InterviewSession

        InterviewSession.objects.filter(pk=self.session.pk).update(
            ended_at=timezone.now() - dt.timedelta(days=days)
        )

    def test_an_old_transcript_is_deleted_and_the_session_survives(self):
        from apps.interviews.models import InterviewSession, InterviewTurn
        from apps.interviews.tasks import purge_old_transcripts

        self.age(days=retention.INTERVIEW_TRANSCRIPT_DAYS + 1)
        self.assertEqual(purge_old_transcripts(), 1)

        self.assertFalse(InterviewTurn.objects.exists())
        self.assertTrue(InterviewSession.objects.filter(pk=self.session.pk).exists())

    def test_a_recent_transcript_is_kept(self):
        from apps.interviews.models import InterviewTurn
        from apps.interviews.tasks import purge_old_transcripts

        self.age(days=30)
        purge_old_transcripts()
        self.assertTrue(InterviewTurn.objects.exists())


class ClosedApplicationRetentionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        from apps.candidates.tests.factories import create_profile
        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.tests.factories import create_job, publish
        from apps.matching.tests.test_matching import application_for

        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = application_for(create_profile(), self.job)

    def close(self, *, days_ago: int) -> None:
        from apps.applications.models import Application

        Application.objects.filter(pk=self.application.pk).update(
            answers={"why": "Because I have wanted this for years."},
            closed_at=timezone.now() - dt.timedelta(days=days_ago),
        )

    def test_an_old_closed_application_keeps_its_shell_and_loses_its_content(self):
        from apps.applications.models import Application
        from apps.applications.tasks import purge_closed_applications

        self.close(days_ago=retention.CLOSED_APPLICATION_DAYS + 1)
        result = purge_closed_applications()

        self.application.refresh_from_db()
        self.assertEqual(result["answers"], 1)
        self.assertEqual(self.application.answers, {})
        # The employer keeps that it happened and how it ended.
        self.assertTrue(Application.objects.filter(pk=self.application.pk).exists())

    def test_a_recently_closed_application_is_untouched(self):
        from apps.applications.tasks import purge_closed_applications

        self.close(days_ago=30)
        purge_closed_applications()

        self.application.refresh_from_db()
        self.assertNotEqual(self.application.answers, {})

    def test_an_open_application_is_never_swept(self):
        # Age is measured from closure: an open application is current data
        # however old the posting is.
        from apps.applications.models import Application
        from apps.applications.tasks import purge_closed_applications

        Application.objects.filter(pk=self.application.pk).update(
            answers={"why": "still waiting"}, applied_at=timezone.now() - dt.timedelta(days=900)
        )
        purge_closed_applications()

        self.application.refresh_from_db()
        self.assertNotEqual(self.application.answers, {})


class ExpiredFlagReportTests(TestCase):
    def test_an_expired_flag_is_reported(self):
        from apps.core.models import FeatureFlag
        from apps.core.tasks import report_expired_flags

        FeatureFlag.objects.create(
            key="old_experiment",
            is_enabled=True,
            expires_on=timezone.localdate() - dt.timedelta(days=1),
        )
        self.assertEqual(report_expired_flags(), ["old_experiment"])

    def test_a_live_flag_is_not(self):
        from apps.core.models import FeatureFlag
        from apps.core.tasks import report_expired_flags

        FeatureFlag.objects.create(
            key="current",
            is_enabled=True,
            expires_on=timezone.localdate() + dt.timedelta(days=30),
        )
        self.assertEqual(report_expired_flags(), [])


class RenderRetentionTests(TestCase):
    """The render rule, end to end (§15.3, `RENDER_DAYS`).

    Worth its own class because the P19 audit found this rule was named in the
    table and enforced by nothing: renders were stored with no `expires_at`, and
    the task the table pointed at only removes what is deleted or expired. The
    schedule test passed the whole time, because it checks that a *task* is
    scheduled — not that the task implements the duration.

    So this asserts the mechanism rather than the wiring: a render is born with
    an expiry, an expired one is released, and a fresh one is left alone.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        from apps.billing.tests import factories as billing_factories
        from apps.candidates.tests.factories import create_profile
        from apps.resumes.services import documents, drafts, renders, versions
        from apps.resumes.tests.test_rendering import sample_content

        self.profile = create_profile()
        self.actor = self.profile.user
        billing_factories.put_on_pro(self.profile)

        document = documents.create_document(profile=self.profile, actor=self.actor, name="CV")
        drafts.autosave(draft=document.draft, content=sample_content())
        document.refresh_from_db()
        version = versions.finalize(document=document, actor=self.actor, reason="exported")
        self.record = renders.render_version(version=version, actor=self.actor)

    def test_a_render_is_born_with_an_expiry(self):
        self.assertIsNotNone(self.record.attachment.expires_at)

    def test_the_expiry_matches_the_retention_rule(self):
        expected = timezone.now() + dt.timedelta(days=retention.RENDER_DAYS)
        self.assertAlmostEqual(
            self.record.attachment.expires_at.timestamp(), expected.timestamp(), delta=120
        )

    def test_a_fresh_render_is_not_purged(self):
        from apps.resumes.tasks import purge_expired_renders

        self.assertEqual(purge_expired_renders(), 0)

    def test_an_expired_render_is_released_and_its_bytes_go(self):
        from apps.files.models import Attachment
        from apps.files.services import purge
        from apps.resumes.models import ResumeRender
        from apps.resumes.tasks import purge_expired_renders

        attachment_id = self.record.attachment_id
        Attachment.objects.filter(pk=attachment_id).update(
            expires_at=timezone.now() - dt.timedelta(days=1)
        )

        self.assertEqual(purge_expired_renders(), 1)
        self.assertFalse(ResumeRender.objects.filter(pk=self.record.pk).exists())

        # The render row is gone, so the attachment is no longer PROTECTed and
        # the file job can remove the bytes.
        purge()
        self.assertFalse(Attachment.objects.filter(pk=attachment_id).exists())

    def test_an_expired_render_is_not_served_as_a_cache_hit(self):
        # It would 404 at download, because the file policy refuses an expired
        # attachment. A re-render is the correct answer.
        from apps.files.models import Attachment
        from apps.resumes.services import renders

        Attachment.objects.filter(pk=self.record.attachment_id).update(
            expires_at=timezone.now() - dt.timedelta(days=1)
        )

        fresh = renders.render_version(version=self.record.version, actor=self.actor)
        self.assertNotEqual(fresh.pk, self.record.pk)
        self.assertTrue(fresh.attachment.is_servable)

    def test_the_file_purge_survives_a_still_protected_attachment(self):
        # Before the fix this raised ProtectedError and took the whole nightly
        # purge down with it.
        from apps.files.models import Attachment
        from apps.files.services import purge

        Attachment.objects.filter(pk=self.record.attachment_id).update(
            expires_at=timezone.now() - dt.timedelta(days=1)
        )
        purge()  # the render still references it; must not raise

        self.assertTrue(Attachment.objects.filter(pk=self.record.attachment_id).exists())
