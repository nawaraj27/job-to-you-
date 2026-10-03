"""
The CV you uploaded follows you round the site (CVP-04).

Manual testing: from the progress page there was no way to close it, carry on
browsing and come back — nothing outside Uploads said a file was still being
read, or that it was done. Asserted here: the top-bar chip on other pages
(running → "Resume progress", done → "View results"), only one poller at a
time, the progress page's Close / Minimize / Continue browsing, and one
notification when the CV has been read.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.candidates.services import profile as profile_service
from apps.files.constants import ScanStatus
from apps.files.tests import factories as file_factories
from apps.notifications.models import Notification
from apps.resumes import progress
from apps.resumes.models import ParseStatus, ResumeUpload


class ChipFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.user = create_user(full_name="Anjali Thapa")
        self.client.force_login(self.user)
        self.profile = profile_service.get_or_create_profile(user=self.user)

    def upload(self):
        response = self.client.post(
            reverse("resume:upload_receive"),
            {"file": file_factories.pdf_upload("anjali-cv.pdf")},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        record = ResumeUpload.objects.get(profile=self.profile)
        record.attachment.scan_status = ScanStatus.PENDING
        record.attachment.save(update_fields=["scan_status"])
        return record

    def jobs_page(self):
        return self.client.get(reverse("jobs:list"))


class ChipTests(ChipFixture):
    def test_nothing_uploaded_no_chip_and_no_query(self):
        request = RequestFactory().get("/")
        request.user = self.user
        request.session = self.client.session

        with self.assertNumQueries(0):
            self.assertIsNone(progress.current(request))
        self.assertNotContains(self.jobs_page(), "Resume progress")

    def test_while_it_is_being_checked_every_page_says_so(self):
        self.upload()

        page = self.jobs_page()

        self.assertContains(page, "Checking your CV")
        self.assertContains(page, "step 1/3")
        self.assertContains(page, reverse("resume:processing"))
        self.assertContains(page, f'hx-get="{reverse("resume:progress_chip")}"')

    def test_the_step_moves_on_while_reading(self):
        record = self.upload()
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        ResumeUpload.objects.filter(pk=record.pk).update(parse_status=ParseStatus.RUNNING)

        chip = self.client.get(reverse("resume:progress_chip"))

        self.assertContains(chip, "Reading your CV")
        self.assertContains(chip, "step 2/3")

    def test_when_done_it_offers_the_results_and_stops_asking(self):
        record = self.upload()
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        ResumeUpload.objects.filter(pk=record.pk).update(parse_status=ParseStatus.PARSED)

        chip = self.client.get(reverse("resume:progress_chip"))

        self.assertContains(chip, "View results")
        self.assertNotContains(chip, "hx-trigger")

    def test_opening_the_results_retires_the_chip(self):
        record = self.upload()
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        ResumeUpload.objects.filter(pk=record.pk).update(parse_status=ParseStatus.PARSED)

        self.client.get(reverse("resume:upload"))

        self.assertNotContains(self.jobs_page(), "View results")

    def test_pages_with_their_own_live_status_do_not_poll_twice(self):
        self.upload()

        for name in ("resume:upload", "resume:processing"):
            with self.subTest(page=name):
                self.assertNotContains(self.client.get(reverse(name)), 'id="cv-progress-chip"')

    def test_another_candidates_upload_is_never_shown(self):
        record = self.upload()
        other = create_user()
        profile_service.get_or_create_profile(user=other)
        self.client.force_login(other)
        session = self.client.session
        session[progress.SESSION_KEY] = [str(record.public_id)]
        session.save()

        self.assertNotContains(self.jobs_page(), "Resume progress")


class ProgressPageTests(ChipFixture):
    def test_close_minimize_and_continue_browsing(self):
        self.upload()

        page = self.client.get(
            reverse("resume:processing"), HTTP_REFERER="http://testserver/applications/"
        )

        self.assertContains(page, 'aria-label="Close. Reading carries on"')
        self.assertContains(page, 'href="/applications/"')
        self.assertContains(page, "Minimize to the top bar")
        self.assertContains(page, "Continue browsing jobs")
        self.assertContains(page, "Step 1 of 3")

    def test_an_off_site_referer_is_not_where_close_goes(self):
        self.upload()

        page = self.client.get(reverse("resume:processing"), HTTP_REFERER="https://evil.test/x")

        self.assertNotContains(page, "evil.test")
        self.assertContains(page, f'href="{reverse("dashboard:index")}"')

    def test_a_read_cv_offers_view_results(self):
        record = self.upload()
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        ResumeUpload.objects.filter(pk=record.pk).update(parse_status=ParseStatus.PARSED)

        self.assertContains(self.client.get(reverse("resume:processing")), "View results")


class ReadNotificationTests(ChipFixture):
    def test_one_notice_when_the_cv_has_been_read_even_if_read_twice(self):
        from apps.companies.tests.test_company_follow import deliver
        from apps.resumes.services import parse as parse_service

        record = self.upload()
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        ResumeUpload.objects.filter(pk=record.pk).update(
            parse_status=ParseStatus.PENDING, claimed_at=record.created_at
        )
        record.refresh_from_db()

        parse_service.parse_upload(upload=record)
        deliver()
        ResumeUpload.objects.filter(pk=record.pk).update(parse_status=ParseStatus.PENDING)
        record.refresh_from_db()
        parse_service.parse_upload(upload=record)
        deliver()

        notices = Notification.objects.filter(recipient=self.user, kind="resume.parsed")
        self.assertEqual(notices.count(), 1)
        notice = notices.get()
        self.assertEqual(notice.title, "Your CV has been read")
        self.assertTrue(notice.target_url.startswith(reverse("resume:upload")))


class PollDoesNotRetireTests(ChipFixture):
    def test_the_uploads_list_poll_does_not_retire_the_chip(self):
        """Found in the browser: the list's own poll hid "View results"."""
        record = self.upload()
        record.attachment.scan_status = ScanStatus.CLEAN
        record.attachment.save(update_fields=["scan_status"])
        ResumeUpload.objects.filter(pk=record.pk).update(parse_status=ParseStatus.PARSED)

        self.client.get(reverse("resume:upload"), HTTP_HX_REQUEST="true")

        self.assertContains(self.jobs_page(), "View results")
