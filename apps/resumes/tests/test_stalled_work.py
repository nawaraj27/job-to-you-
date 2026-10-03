"""
Work whose task was lost is recovered, not left "waiting" for ever (STALL-01).

Found during the 2026-09-21 verification pass: the demo company's certificate
sat at "cannot be opened: pending" because its scan task was queued, the broker
restarted, and nothing ever asked again. The same loss leaves a CV at "Waiting
to be parsed" and a report at "the written review is taking longer than
usual" — states the screens faithfully show and the candidate can do nothing
about.
"""

from __future__ import annotations

import datetime as dt
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.candidates.services import profile as profile_service
from apps.files.constants import ScanStatus
from apps.files.models import Attachment
from apps.files.tests import factories as file_factories
from apps.resumes.models import EnrichmentState, ParseStatus, ResumeAnalysis, ResumeUpload
from apps.resumes.services import upload as upload_service
from config.celery import app as celery_app


class StalledWorkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.user = create_user()
        self.profile = profile_service.get_or_create_profile(user=self.user)
        self.upload, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.user, uploaded_file=file_factories.pdf_upload()
        )
        self.long_ago = timezone.now() - dt.timedelta(hours=2)

    def test_a_scan_that_never_ran_is_asked_for_again(self):
        from apps.files import tasks

        Attachment.objects.filter(pk=self.upload.attachment_id).update(
            scan_status=ScanStatus.PENDING, updated_at=self.long_ago
        )

        with mock.patch.object(tasks.scan_attachment, "delay") as delay:
            self.assertEqual(tasks.requeue_stalled_scans(), 1)

        delay.assert_called_once_with(self.upload.attachment_id)

    def test_a_scan_in_progress_is_left_alone(self):
        from apps.files import tasks

        Attachment.objects.filter(pk=self.upload.attachment_id).update(
            scan_status=ScanStatus.PENDING, updated_at=timezone.now()
        )

        with mock.patch.object(tasks.scan_attachment, "delay") as delay:
            self.assertEqual(tasks.requeue_stalled_scans(), 0)
        delay.assert_not_called()

    def test_a_read_that_never_ran_is_asked_for_again(self):
        from apps.resumes import tasks

        Attachment.objects.filter(pk=self.upload.attachment_id).update(scan_status=ScanStatus.CLEAN)
        ResumeUpload.objects.filter(pk=self.upload.pk).update(
            parse_status=ParseStatus.PENDING, updated_at=self.long_ago
        )

        with mock.patch.object(tasks.parse_upload, "delay") as delay:
            result = tasks.recover_stalled_resume_work()

        self.assertEqual(result["reads"], 1)
        delay.assert_called_once_with(self.upload.pk)

    def test_a_review_that_is_not_coming_becomes_retryable(self):
        from apps.billing.tests import factories as billing_factories
        from apps.resumes import examples, tasks
        from apps.resumes.services import analysis as analysis_service
        from apps.resumes.services import documents as document_service

        billing_factories.put_on_pro(self.profile)
        document = document_service.create_document(
            profile=self.profile,
            actor=self.user,
            name="CV",
            content=examples.example_content(name="Anjali", email="a@example.test"),
        )
        stalled = analysis_service.analyse(document=document, actor=self.user, defer_ai=True)
        fresh = analysis_service.analyse(document=document, actor=self.user, defer_ai=True)
        ResumeAnalysis.objects.filter(pk__in=[stalled.pk, fresh.pk]).update(
            enrichment=EnrichmentState.PENDING
        )
        ResumeAnalysis.objects.filter(pk=stalled.pk).update(created_at=self.long_ago)

        self.assertEqual(tasks.recover_stalled_resume_work()["reviews"], 1)

        stalled.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stalled.enrichment, EnrichmentState.FAILED)
        self.assertEqual(fresh.enrichment, EnrichmentState.PENDING)

    def test_both_sweeps_are_scheduled(self):
        scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

        self.assertIn("apps.files.tasks.requeue_stalled_scans", scheduled)
        self.assertIn("apps.resumes.tasks.recover_stalled_resume_work", scheduled)
