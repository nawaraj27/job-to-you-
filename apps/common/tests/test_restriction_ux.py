"""
An expected "no" must not look like the site has broken.

Two defects, both found by reproducing a free candidate's second analysis of
the month:

  * On an htmx action whose view let the refusal reach the middleware, the
    402 came back as plain text; htmx does not swap error statuses, so the
    client's network-failure handler painted "The connection dropped or the
    server didn't answer" over it.
  * On a screen whose view caught the refusal itself, the shared flash partial
    printed every `DomainError` as a red "That didn't work".

What is asserted: refusals that are product rules render as restrictions with
the service's sentence and a real next step; real failures keep their error
treatment; and the server still refuses — nothing here is a bypass.
"""

from __future__ import annotations

from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.billing.services.entitlements import PlanLimitExceeded
from apps.candidates.services import profile as profile_service
from apps.common import restrictions
from apps.common.errors import InvalidState, NotAllowed, ProviderError
from apps.common.errors_middleware import DomainErrorMiddleware
from apps.resumes.models import ResumeAnalysis
from apps.resumes.services import analysis as analysis_service
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service


def _middleware() -> DomainErrorMiddleware:
    return DomainErrorMiddleware(lambda request: HttpResponse("ok"))


class ClassificationTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")

    def test_a_plan_limit_is_a_restriction_that_points_at_plans(self):
        found = restrictions.describe(PlanLimitExceeded("One a month."), self.request)

        self.assertEqual(found.kind, "plan")
        self.assertEqual(found.title, "Not included in your plan")
        self.assertEqual(found.action_url, reverse("dashboard:plan"))

    def test_permission_and_state_refusals_are_restrictions(self):
        self.assertEqual(restrictions.describe(NotAllowed(), self.request).kind, "permission")
        self.assertEqual(restrictions.describe(InvalidState(), self.request).kind, "state")

    def test_a_provider_outage_is_not_dressed_up_as_a_restriction(self):
        self.assertIsNone(restrictions.describe(ProviderError(), self.request))


class MiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_an_htmx_plan_refusal_is_swappable_content_with_its_real_status(self):
        request = self.factory.post("/", HTTP_HX_REQUEST="true")
        request.user = create_user()

        response = _middleware().process_exception(request, PlanLimitExceeded("One a month."))

        self.assertEqual(response.status_code, 402)
        self.assertEqual(response["X-Restriction"], "plan")
        body = response.content.decode()
        self.assertIn("Not included in your plan", body)
        self.assertIn("One a month.", body)
        self.assertIn(reverse("dashboard:plan"), body)
        self.assertNotIn("connection dropped", body)

    def test_an_htmx_provider_failure_keeps_the_error_treatment(self):
        request = self.factory.post("/", HTTP_HX_REQUEST="true")
        request.user = create_user()

        response = _middleware().process_exception(request, ProviderError())

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("X-Restriction", response)
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_a_page_refusal_is_a_page_with_a_way_on(self):
        request = self.factory.get("/")
        request.user = create_user()
        request.session = {}
        from django.contrib.messages.storage.fallback import FallbackStorage

        request._messages = FallbackStorage(request)

        response = _middleware().process_exception(request, PlanLimitExceeded("One a month."))

        self.assertEqual(response.status_code, 402)
        body = response.content.decode()
        self.assertIn("<html", body.lower())
        self.assertIn("View plans", body)
        self.assertIn(reverse("core:home"), body)

    def test_anything_that_is_not_a_domain_error_is_left_alone(self):
        request = self.factory.get("/")

        self.assertIsNone(_middleware().process_exception(request, RuntimeError("bug")))


class ScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.user = create_user(full_name="Anjali Thapa")
        self.client.force_login(self.user)
        self.profile = profile_service.get_or_create_profile(user=self.user)
        self.document = document_service.create_document(
            profile=self.profile, actor=self.user, name="Ops CV"
        )
        draft_service.set_contact(draft=self.document.draft, field="name", value="Anjali Thapa")
        self.document.refresh_from_db()
        # The free plan's one analysis this month, spent.
        analysis_service.analyse(document=self.document, actor=self.user, defer_ai=True)

    def test_a_second_analysis_on_the_free_plan_reads_as_a_plan_restriction(self):
        response = self.client.post(
            reverse("resume:analysis_run"),
            {"document": str(self.document.public_id)},
            HTTP_HX_REQUEST="true",
        )

        # RES-03: the refusal keeps its real status, with the header the
        # client swaps on. It used to come back 200.
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response["X-Restriction"], "plan")
        # A metered allowance used up, so it says the limit was reached (ADR-A59)
        # rather than that the feature is not part of the plan.
        self.assertContains(response, "You&#x27;ve reached your plan&#x27;s limit", status_code=402)
        self.assertContains(response, reverse("dashboard:plan"), status_code=402)
        self.assertContains(response, 'data-restriction="plan"', status_code=402)
        self.assertNotContains(response, "That didn&#x27;t work", status_code=402)

    def test_the_server_still_refuses(self):
        self.client.post(
            reverse("resume:analysis_run"),
            {"document": str(self.document.public_id)},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(ResumeAnalysis.objects.filter(version__document=self.document).count(), 1)


class EmployerNextStepTests(TestCase):
    """Who is told what, inside a company (verification 2026-09-21).

    Before: a Company Admin — who may open billing and see the plan (D-11) —
    got "Only the company owner can change the plan" with no link to the plan
    they can see, and a permission refusal said nothing about who could help.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        from apps.companies.models import Role
        from apps.companies.tests.factories import company_with_owner

        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner, name="Kora Studio")
        self.admin_membership = self._member(Role.ADMIN)
        self.recruiter_membership = self._member(Role.RECRUITER)

    def _member(self, role):
        from apps.companies.tests.factories import add_member

        return add_member(self.company, create_user(), role=role)

    def request_for(self, membership):
        request = RequestFactory().get("/")
        request.company = self.company
        request.membership = membership
        return request

    def test_the_owner_is_sent_to_billing_to_change_it(self):
        found = restrictions.describe(
            PlanLimitExceeded("Not on this plan."), self.request_for(self.owner_membership)
        )

        self.assertEqual(found.action_label, "Go to billing")
        self.assertEqual(found.action_url, reverse("employer:billing", args=[self.company.slug]))

    def test_an_admin_is_shown_the_plan_and_told_who_changes_it(self):
        found = restrictions.describe(
            PlanLimitExceeded("Not on this plan."), self.request_for(self.admin_membership)
        )

        self.assertEqual(found.action_label, "See the plan")
        self.assertEqual(found.action_url, reverse("employer:billing", args=[self.company.slug]))
        self.assertIn("Only the owner of Kora Studio", found.hint)

    def test_a_recruiter_gets_no_link_they_cannot_open_and_is_told_who_can(self):
        found = restrictions.describe(
            PlanLimitExceeded("Not on this plan."), self.request_for(self.recruiter_membership)
        )

        self.assertEqual(found.action_url, "")
        self.assertIn("Ask them", found.hint)

    def test_a_permission_refusal_says_who_can_change_the_role(self):
        found = restrictions.describe(NotAllowed(), self.request_for(self.recruiter_membership))

        self.assertEqual(found.kind, "permission")
        self.assertIn("An owner or admin can change your role", found.hint)

    def test_an_admin_refused_something_is_not_told_to_ask_themselves(self):
        found = restrictions.describe(NotAllowed(), self.request_for(self.admin_membership))

        self.assertEqual(found.hint, "")

    def test_the_billing_link_really_opens_for_the_admin_and_not_the_recruiter(self):
        url = reverse("employer:billing", args=[self.company.slug])

        self.client.force_login(self.admin_membership.user)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.recruiter_membership.user)
        self.assertIn(self.client.get(url).status_code, (403, 404))

    def test_a_refusal_a_view_redirects_with_is_a_warning_not_an_error(self):
        """RES-04: a Viewer opening "New job" landed under a red role=alert box."""
        from apps.companies.models import Role

        viewer = self._member(Role.VIEWER)
        self.client.force_login(viewer.user)

        response = self.client.get(
            reverse("employer:job_create", args=[self.company.slug]), follow=True
        )

        self.assertContains(response, "alert alert-warning")
        self.assertContains(response, "Ask an owner or admin")
        self.assertNotContains(response, "alert alert-error")

    def test_a_real_failure_redirected_back_is_still_an_error(self):
        from django.contrib.messages import get_messages

        from apps.common.errors import ProviderError

        request = RequestFactory().get("/")
        request.session = self.client.session
        from django.contrib.messages.storage.fallback import FallbackStorage

        request._messages = FallbackStorage(request)
        restrictions.flash(request, ProviderError("The payment provider did not answer."))

        levels = [message.level_tag for message in get_messages(request)]
        self.assertEqual(levels, ["error"])
