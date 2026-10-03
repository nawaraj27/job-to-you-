"""
Hostile and empty form input never produces a server error (whole-site loop).

`scripts/post_sweep.py` posted empty, unknown, non-numeric, negative, huge,
UUID-shaped and 5,000-character values to every action endpoint and found 123
server errors on eight of them. Two causes:

  * raw form values reached UUID columns (`public_id`) and the ORM raised
    `ValidationError` — the CV builder, CV analysis, apply, practice
    interviews, bulk applicant moves and webhook settings;
  * company create and edit sent unchecked lengths and foreign keys to
    PostgreSQL.

These tests pin the endpoints that failed, and the two central guards.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.candidates.tests.factories import create_profile
from apps.common.errors_middleware import DomainErrorMiddleware
from apps.common.ids import as_uuid
from apps.companies.models import Company
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish

JUNK = ["", "abc", "-1", "99999999999999999999", "x" * 5000]


class AsUuidTests(TestCase):
    def test_a_real_uuid_comes_back_canonical(self):
        value = uuid.uuid4()
        self.assertEqual(as_uuid(str(value).upper()), str(value))
        self.assertEqual(as_uuid(value), str(value))

    def test_anything_else_is_none(self):
        for value in [*JUNK, None, 12, "00000000-0000-0000-0000-00000000000Z"]:
            with self.subTest(value=value):
                self.assertIsNone(as_uuid(value))


class MiddlewareSafetyNetTests(TestCase):
    def setUp(self):
        self.middleware = DomainErrorMiddleware(lambda request: HttpResponse())

    def request(self, **headers):
        request = RequestFactory().post("/somewhere/", HTTP_HOST="testserver", **headers)
        from django.contrib.messages.storage.fallback import FallbackStorage

        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_a_stray_validation_error_is_a_400_not_a_500(self):
        response = self.middleware.process_exception(
            self.request(), ValidationError("'abc' is not a valid UUID.")
        )

        self.assertEqual(response.status_code, 400)

    def test_an_error_redirect_stays_on_this_site(self):
        from apps.common.errors import InvalidState

        home = self.middleware.process_exception(
            self.request(HTTP_REFERER="http://testserver/settings/"), InvalidState("no")
        )
        away = self.middleware.process_exception(
            self.request(HTTP_REFERER="https://evil.example/phish"), InvalidState("no")
        )

        self.assertEqual(home.status_code, 302)
        self.assertEqual(home["Location"], "http://testserver/settings/")
        self.assertNotEqual(away.status_code, 302)


class CandidateEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.client.force_login(self.profile.user)
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        self.job = publish(create_job(company))

    def assert_no_server_error(self, url, field):
        for value in JUNK:
            with self.subTest(url=url, value=value[:20]):
                response = self.client.post(url, {field: value, "command": value, "action": value})
                self.assertLess(response.status_code, 500)

    def test_the_cv_builder_document_actions(self):
        self.assert_no_server_error(reverse("resume:document_act"), "document")

    def test_running_a_cv_analysis(self):
        self.assert_no_server_error(reverse("resume:analysis_run"), "document")

    def test_applying(self):
        self.assert_no_server_error(reverse("jobs:apply", args=[self.job.slug]), "document")

    def test_starting_a_practice_interview(self):
        self.assert_no_server_error(reverse("interviews:practice"), "job")


class EmployerEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.client.force_login(self.owner)

    def test_bulk_applicant_actions_with_junk_ids(self):
        url = reverse("employer:applicants", args=[self.company.slug])
        for value in JUNK:
            with self.subTest(value=value[:20]):
                response = self.client.post(url, {"application": [value], "stage": value})
                self.assertLess(response.status_code, 500)

    def test_webhook_settings_with_junk_ids(self):
        url = reverse("employer:webhooks", args=[self.company.slug])
        for value in JUNK:
            with self.subTest(value=value[:20]):
                response = self.client.post(url, {"action": "delete", "endpoint": value})
                self.assertLess(response.status_code, 500)

    def test_a_company_name_longer_than_the_column_is_a_message(self):
        response = self.client.post(reverse("employer:create"), {"name": "N" * 500})

        self.assertLess(response.status_code, 500)
        self.assertFalse(Company.objects.filter(name__startswith="NNNN").exists())

    def test_a_blank_company_name_is_refused(self):
        from apps.common.errors import InvalidState
        from apps.companies.services import company as company_service

        with self.assertRaises(InvalidState):
            company_service.create_company(actor=self.owner, name="   ")

    def test_profile_edits_are_checked_before_the_database(self):
        from apps.common.errors import InvalidState
        from apps.companies.services import company as company_service

        for fields in (
            {"tagline": "t" * 300},
            {"name": ""},
            {"industry_id": "999999"},
            {"location_id": "999999"},
        ):
            with self.subTest(fields=list(fields)), self.assertRaises(InvalidState):
                self.company.refresh_from_db()
                company_service.update_company(
                    actor=self.owner,
                    company=self.company,
                    row_version=self.company.row_version,
                    **fields,
                )
