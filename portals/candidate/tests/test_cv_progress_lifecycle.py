"""
A CV that is being read must still be findable an hour later.

The bug this file was written for: a candidate uploads a CV, follows "watch
progress", then goes to look at jobs — and has no way back. The progress page
is reachable from the upload confirmation alert and from nothing else, the
confirmation alert is gone the moment they navigate, and the candidate rail
has no entry for the CV they just handed over. The work carries on server-side
the whole time; the candidate simply cannot see it.

So what is asserted here is *reachability and continuity*, not the pipeline
itself (`apps/resumes/tests/test_ingestion.py` owns that):

  * the rail offers a way back to an upload in progress;
  * leaving the progress page cancels nothing;
  * coming back shows the real server-side state rather than restarting;
  * a finished analysis is reachable from where the candidate left off;
  * one candidate's upload and analysis are invisible to another.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.candidates.services import profile as profile_service
from apps.core import navigation
from apps.files.constants import ScanStatus
from apps.files.tests import factories as file_factories
from apps.resumes.models import ParseStatus, ResumeUpload
from apps.resumes.services import upload as upload_service


class ProgressFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.user = create_user(full_name="Anjali Thapa")
        self.client.force_login(self.user)
        self.profile = profile_service.get_or_create_profile(user=self.user)

    def upload(self, name: str = "anjali-cv.pdf") -> ResumeUpload:
        """One owned upload, still being scanned — the state the bug is about."""
        record, _created = upload_service.upload_for_profile(
            profile=self.profile, actor=self.user, uploaded_file=file_factories.pdf_upload(name)
        )
        record.attachment.scan_status = ScanStatus.PENDING
        record.attachment.save(update_fields=["scan_status"])
        record.parse_status = ParseStatus.PENDING
        record.save(update_fields=["parse_status"])
        record.original_filename = name
        record.save(update_fields=["original_filename"])
        self.upload_id = record.public_id
        return record


class ReachabilityTests(ProgressFixture):
    def test_the_rail_offers_a_way_back_to_an_upload(self):
        """The whole bug in one assertion.

        Without a rail entry the progress page is reachable only from the
        confirmation alert, which does not survive navigating away.
        """
        urls = {
            link["url"] for group in navigation.rail_nav("candidate") for link in group["links"]
        }

        self.assertIn(reverse("resume:upload"), urls)

    def test_the_upload_screen_lists_an_upload_that_is_still_being_read(self):
        record = self.upload()

        response = self.client.get(reverse("resume:upload"))

        self.assertContains(response, record.original_filename)

    def test_the_progress_screen_offers_a_way_out_that_is_not_a_deletion(self):
        self.upload()

        response = self.client.get(reverse("resume:processing"))

        self.assertContains(response, "Back to your uploads")
        self.assertContains(response, "carries on without you")
        # Leaving is not cancelling, and this screen offers no way to cancel:
        # removing an upload lives on the uploads screen behind a confirm.
        self.assertNotContains(response, reverse("resume:upload_delete", args=[self.upload_id]))

    def test_the_uploads_screen_says_the_work_continues_without_it(self):
        self.upload()

        response = self.client.get(reverse("resume:upload"))

        self.assertContains(response, "carries on whether this page is open or not")
        self.assertContains(response, 'hx-trigger="every 2s"')

    def test_the_uploads_screen_stops_refreshing_once_nothing_is_moving(self):
        record = self.upload()
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        record.parse_status = ParseStatus.PARSED
        record.save(update_fields=["parse_status"])

        response = self.client.get(reverse("resume:upload"))

        self.assertNotContains(response, 'hx-trigger="every 2s"')

    def test_leaving_the_progress_screen_cancels_nothing(self):
        record = self.upload()

        self.client.get(reverse("resume:processing"))
        self.client.get(reverse("jobs:list"))
        self.client.get(reverse("dashboard:index"))
        record.refresh_from_db()

        self.assertEqual(record.parse_status, ParseStatus.PENDING)
        self.assertIsNone(record.attachment.deleted_at)

    def test_reopening_progress_shows_the_current_state_and_creates_nothing(self):
        record = self.upload()

        self.client.get(reverse("resume:processing"))
        # The scanner clears it while the candidate is elsewhere.
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        record.parse_status = ParseStatus.RUNNING
        record.save(update_fields=["parse_status"])

        response = self.client.get(reverse("resume:processing"))

        self.assertContains(response, "Parsing")
        self.assertEqual(ResumeUpload.objects.filter(profile=self.profile).count(), 1)


class ReturningTests(ProgressFixture):
    def test_home_says_where_an_unfinished_upload_is(self):
        self.upload()

        response = self.client.get(reverse("dashboard:index"))

        self.assertContains(response, "Still reading anjali-cv.pdf")
        self.assertContains(response, reverse("resume:upload"))

    def test_home_says_nothing_once_it_has_finished(self):
        record = self.upload()
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        record.parse_status = ParseStatus.PARSED
        record.save(update_fields=["parse_status"])

        self.assertNotContains(self.client.get(reverse("dashboard:index")), "Still reading")

    def test_a_finished_report_is_reachable_and_reading_it_runs_nothing(self):
        from apps.resumes.models import ResumeAnalysis
        from apps.resumes.services import analysis as analysis_service
        from apps.resumes.services import documents as document_service
        from apps.resumes.services import drafts as draft_service

        document = document_service.create_document(
            profile=self.profile, actor=self.user, name="Ops CV"
        )
        draft_service.set_contact(draft=document.draft, field="name", value="Anjali Thapa")
        document.refresh_from_db()
        analysis = analysis_service.analyse(document=document, actor=self.user, defer_ai=True)

        url = reverse("resume:analysis") + f"?document={document.public_id}"
        for _ in range(3):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

        self.assertEqual(ResumeAnalysis.objects.filter(version__document=document).count(), 1)
        self.assertEqual(
            self.client.get(
                reverse("resume:analysis") + f"?report={analysis.public_id}"
            ).status_code,
            200,
        )


class IsolationTests(ProgressFixture):
    def test_one_cv_never_shows_another_cvs_report(self):
        from apps.billing.tests import factories as billing_factories
        from apps.resumes.services import analysis as analysis_service
        from apps.resumes.services import documents as document_service
        from apps.resumes.services import drafts as draft_service

        # Two analyses in one month is a Pro allowance; the gate is asserted
        # in apps/billing/tests.
        billing_factories.put_on_pro(self.profile)
        first = document_service.create_document(profile=self.profile, actor=self.user, name="A")
        second = document_service.create_document(profile=self.profile, actor=self.user, name="B")
        draft_service.set_contact(draft=first.draft, field="name", value="Alpha Name")
        draft_service.set_contact(draft=second.draft, field="name", value="Bravo Name")
        first.refresh_from_db()
        second.refresh_from_db()
        report_a = analysis_service.analyse(document=first, actor=self.user, defer_ai=True)
        report_b = analysis_service.analyse(document=second, actor=self.user, defer_ai=True)

        page = self.client.get(reverse("resume:analysis") + f"?document={first.public_id}")

        self.assertEqual(page.context["analysis"].pk, report_a.pk)
        self.assertNotEqual(page.context["analysis"].pk, report_b.pk)

    def test_another_candidate_cannot_open_the_report(self):
        from apps.resumes.services import analysis as analysis_service
        from apps.resumes.services import documents as document_service
        from apps.resumes.services import drafts as draft_service

        document = document_service.create_document(profile=self.profile, actor=self.user, name="A")
        draft_service.set_contact(draft=document.draft, field="name", value="Anjali Thapa")
        document.refresh_from_db()
        report = analysis_service.analyse(document=document, actor=self.user, defer_ai=True)
        other = create_user()
        profile_service.get_or_create_profile(user=other)
        self.client.force_login(other)

        response = self.client.get(reverse("resume:analysis") + f"?report={report.public_id}")

        self.assertEqual(response.status_code, 404)

    def test_another_candidate_sees_none_of_it(self):
        self.upload()
        other = create_user()
        profile_service.get_or_create_profile(user=other)
        self.client.force_login(other)

        for name in ("resume:upload", "resume:processing"):
            with self.subTest(screen=name):
                response = self.client.get(reverse(name))
                self.assertNotContains(response, "anjali-cv.pdf")


class ReviewPollTests(ProgressFixture):
    """The report stops re-rendering itself once the review is overdue (CVP-02).

    Found walking the demo: with no worker running the written review stays
    "pending", and the report re-rendered every three seconds for ever —
    every button on it was replaced under the pointer.
    """

    def setUp(self):
        super().setUp()
        from apps.resumes.services import analysis as analysis_service
        from apps.resumes.services import documents as document_service
        from apps.resumes.services import drafts as draft_service

        self.document = document_service.create_document(
            profile=self.profile, actor=self.user, name="Ops CV"
        )
        draft_service.set_contact(draft=self.document.draft, field="name", value="Anjali")
        self.document.refresh_from_db()
        self.analysis = analysis_service.analyse(
            document=self.document, actor=self.user, defer_ai=True
        )
        self.analysis.enrichment = "pending"
        self.analysis.save(update_fields=["enrichment"])
        self.url = reverse("resume:analysis") + f"?document={self.document.public_id}"

    def test_a_fresh_review_is_polled_for(self):
        self.assertContains(self.client.get(self.url), 'hx-trigger="every 3s"')

    def test_an_overdue_review_is_not_polled_and_says_so(self):
        import datetime as dt

        from django.utils import timezone

        from apps.resumes.models import ResumeAnalysis

        ResumeAnalysis.objects.filter(pk=self.analysis.pk).update(
            created_at=timezone.now() - dt.timedelta(minutes=10)
        )

        response = self.client.get(self.url)

        self.assertNotContains(response, 'hx-trigger="every 3s"')
        self.assertContains(response, "taking longer than usual")

    def test_an_overdue_review_can_be_checked_again_and_the_server_answers(self):
        """Polling stopping is not the end: the backend is the source of truth.

        The review lands after the page gave up; "Check again" (or any reload)
        shows it.
        """
        import datetime as dt

        from django.utils import timezone

        from apps.resumes.models import ResumeAnalysis

        ResumeAnalysis.objects.filter(pk=self.analysis.pk).update(
            created_at=timezone.now() - dt.timedelta(minutes=30)
        )
        late = self.client.get(self.url)
        self.assertContains(late, "Check again")
        self.assertContains(late, f"?report={self.analysis.public_id}")

        ResumeAnalysis.objects.filter(pk=self.analysis.pk).update(
            enrichment="done", headline="The written review, arrived late"
        )
        again = self.client.get(reverse("resume:analysis") + f"?report={self.analysis.public_id}")

        self.assertContains(again, "The written review, arrived late")
        self.assertNotContains(again, "taking longer than usual")
        self.assertEqual(ResumeAnalysis.objects.filter(version__document=self.document).count(), 1)


class ReviewWatcherTests(ReviewPollTests):
    """POLL-01: waiting for the review does not re-render the report.

    Found clicking through the report in the browser: "Answer questions about
    these" was replaced between finding it and clicking it, because the whole
    report swapped itself every three seconds while the review was pending.
    """

    def test_the_report_region_itself_does_not_poll(self):
        import re

        body = self.client.get(self.url).content.decode()
        opening = re.search(r'<div id="analysis-report"[^>]*>', body).group(0)

        self.assertNotIn("hx-trigger", opening)
        self.assertIn('id="review-watch"', body)

    def test_the_watcher_asks_about_itself_while_the_review_is_coming(self):
        response = self.client.get(
            reverse("resume:analysis") + f"?report={self.analysis.public_id}&watch=1"
        )

        self.assertContains(response, 'hx-trigger="every 3s"')
        self.assertNotContains(response, 'hx-trigger="load"')

    def test_when_the_review_lands_the_watcher_reloads_the_report_once(self):
        from apps.resumes.models import ResumeAnalysis

        ResumeAnalysis.objects.filter(pk=self.analysis.pk).update(enrichment="done")

        watched = self.client.get(
            reverse("resume:analysis") + f"?report={self.analysis.public_id}&watch=1"
        )
        self.assertContains(watched, 'hx-trigger="load"')
        self.assertContains(watched, 'hx-target="#analysis-report"')

        # The report that load brings back has no watcher at all.
        final = self.client.get(reverse("resume:analysis") + f"?report={self.analysis.public_id}")
        self.assertNotContains(final, 'id="review-watch"')


class UploadScreenTests(ProgressFixture):
    """What the upload screen does after the Upload button (UPL-01).

    Found in the browser on 2026-09-21: the upload's answer carried a second
    `#upload-history` list into the alert region. The page then had two lists
    — the new one never refreshed, and the original kept saying "Nothing
    uploaded yet." — until the candidate reloaded.
    """

    def post_upload(self):
        return self.client.post(
            reverse("resume:upload_receive"),
            {"file": file_factories.pdf_upload("second-cv.pdf")},
            HTTP_HX_REQUEST="true",
        )

    def test_the_history_replaces_the_list_on_the_page_out_of_band(self):
        response = self.post_upload()

        body = response.content.decode()
        self.assertEqual(body.count('id="upload-history"'), 1)
        self.assertIn('hx-swap-oob="outerHTML"', body)

    def test_the_answer_keeps_refreshing_while_the_file_is_in_flight(self):
        response = self.post_upload()
        record = ResumeUpload.objects.get(profile=self.profile)
        if record.attachment.scan_status != ScanStatus.PENDING:
            record.attachment.scan_status = ScanStatus.PENDING
            record.attachment.save(update_fields=["scan_status"])
            response = self.post_upload()

        self.assertContains(response, 'hx-trigger="every 2s"')

    def test_the_refresh_also_brings_back_what_was_found(self):
        self.upload()

        response = self.client.get(reverse("resume:upload"))

        self.assertContains(response, 'hx-select-oob="#import-proposals"')

    def test_removing_one_file_keeps_refreshing_for_another_still_in_flight(self):
        first = self.upload("first.pdf")
        second, _ = upload_service.upload_for_profile(
            profile=self.profile,
            actor=self.user,
            uploaded_file=file_factories.docx_upload("other.docx"),
        )
        second.attachment.scan_status = ScanStatus.PENDING
        second.attachment.save(update_fields=["scan_status"])

        response = self.client.post(
            reverse("resume:upload_delete", kwargs={"upload_id": first.public_id}),
            HTTP_HX_REQUEST="true",
        )

        self.assertContains(response, 'hx-trigger="every 2s"')

    def test_the_progress_page_stops_asking_once_nothing_is_moving(self):
        record = self.upload()
        polling = self.client.get(reverse("resume:processing"))
        self.assertContains(polling, 'id="processing"')
        self.assertContains(polling, 'hx-select="#processing"')

        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        ResumeUpload.objects.filter(pk=record.pk).update(parse_status=ParseStatus.PARSED)

        # The answer to the wrapper's own poll replaces the wrapper with one
        # that has no trigger — which is what stops the asking.
        done = self.client.get(reverse("resume:processing"))
        self.assertContains(done, 'id="processing"')
        self.assertNotContains(done, 'hx-trigger="every 2s"')


class LatestAnalysisTests(ProgressFixture):
    """REF-01: "Latest analysis" opens the latest analysis, and a refresh keeps it.

    Found in the browser: a second CV analysed, the page left, and "Latest
    analysis" in the rail opened the *main* CV's report instead; the address
    bar never named the report on screen, so a refresh lost it too.
    """

    def setUp(self):
        super().setUp()
        from apps.billing.tests import factories as billing_factories
        from apps.resumes import examples
        from apps.resumes.services import documents as document_service

        billing_factories.put_on_pro(self.profile)
        self.main = document_service.create_document(
            profile=self.profile,
            actor=self.user,
            name="Main CV",
            content=examples.example_content(name="Anjali Thapa", email="a@example.test"),
        )
        self.main.is_primary = True
        self.main.save(update_fields=["is_primary"])
        self.second = document_service.create_document(
            profile=self.profile,
            actor=self.user,
            name="Second CV",
            content=examples.example_content(name="Anjali Thapa", email="a@example.test"),
        )

    def run_analysis(self, document):
        return self.client.post(
            reverse("resume:analysis_run"),
            {"document": str(document.public_id)},
            HTTP_HX_REQUEST="true",
        )

    def test_the_rail_opens_the_newest_report_not_the_main_cv(self):
        self.run_analysis(self.main)
        self.run_analysis(self.second)

        response = self.client.get(reverse("resume:analysis"))

        self.assertEqual(response.context["document"], self.second)

    def test_after_a_run_the_address_names_the_report(self):
        from apps.resumes.models import ResumeAnalysis

        response = self.run_analysis(self.second)

        analysis = ResumeAnalysis.objects.get(version__document=self.second)
        self.assertEqual(
            response["HX-Replace-Url"],
            reverse("resume:analysis") + f"?report={analysis.public_id}",
        )
        refreshed = self.client.get(response["HX-Replace-Url"])
        self.assertEqual(refreshed.context["analysis"], analysis)

    def test_a_named_document_still_wins(self):
        self.run_analysis(self.second)

        response = self.client.get(reverse("resume:analysis") + f"?document={self.main.public_id}")

        self.assertEqual(response.context["document"], self.main)

    def test_navigation_and_refresh_never_start_another_analysis(self):
        from apps.resumes.models import ResumeAnalysis

        self.run_analysis(self.second)
        for url in (
            reverse("resume:analysis"),
            reverse("resume:upload"),
            reverse("resume:processing"),
            reverse("jobs:list"),
            reverse("resume:analysis"),
        ):
            self.client.get(url)

        self.assertEqual(ResumeAnalysis.objects.filter(version__document=self.second).count(), 1)
