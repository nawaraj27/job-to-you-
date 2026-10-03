"""
Every public page still renders.

The frontend was built before the backend and is treated as a fixed contract
(SRS §0.1). Each phase rewires some of these views from session-backed mock
state to real services, so a cheap check that nothing 500s is worth more here
than it would be on a greenfield project — it catches the class of mistake a
phase migration actually makes.

Deliberately shallow: it asserts that a page renders, not what it says. Content
assertions belong to the phase that makes the content real.
"""

from __future__ import annotations

from django.test import TestCase

MARKETING = [
    "/",
    "/pricing/",
    "/about/",
    "/contact/",
    "/privacy/",
    "/terms/",
]

AUTH = [
    "/accounts/login/",
    "/accounts/signup/",
]

CANDIDATE = [
    "/cv/upload/",
    "/cv/analysis/",
    "/cv/builder/",
    "/cv/builder/guided/",
    "/cv/builder/editor/",
    "/cv/templates/",
    "/jobs/",
    "/jobs/saved/",
    # `/jobs/matched/` is deliberately absent: the mock screen behind it went
    # with `apps/jobs/samples.py` in P7, and matching is P13. Its navigation
    # entry is gated on the `job_matching` flag, which is off, so nothing links
    # to a route that is not there.
    "/dashboard/",
    "/dashboard/profile/",
    "/applications/",
    "/applications/visibility/",
    "/notifications/",
]

#: Only the routes that need no company slug. Everything else in the employer
#: portal lives under `/employer/<company_slug>/` (ADR-A18) and is covered by
#: `portals/employer/tests`, which can create a company to scope them to.
EMPLOYER = [
    "/for-employers/",
    "/employer/",
    "/employer/new/",
]

SETTINGS = [
    "/settings/",
    "/settings/notifications/",
    "/settings/privacy/",
    "/settings/appearance/",
    "/settings/data/",
]

OPERATIONAL = [
    "/healthz",
    "/readyz",
]


class PageSmokeTests(TestCase):
    def _assert_renders(self, paths: list[str]) -> None:
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path, follow=True)
                self.assertEqual(
                    response.status_code, 200, f"{path} returned {response.status_code}"
                )

    def test_marketing_pages_render(self):
        self._assert_renders(MARKETING)

    def test_auth_pages_render(self):
        self._assert_renders(AUTH)

    def test_candidate_pages_render(self):
        self._assert_renders(CANDIDATE)

    def test_employer_pages_render(self):
        self._assert_renders(EMPLOYER)

    def test_settings_pages_render(self):
        self._assert_renders(SETTINGS)

    def test_operational_endpoints_render(self):
        self._assert_renders(OPERATIONAL)

    def test_feature_flags_reach_the_rendered_navigation(self):
        # The flag layer runs in a context processor on every request; a failure
        # there breaks every page at once, so it is asserted through a real one.
        from datetime import timedelta

        from django.utils import timezone

        from apps.core import features
        from apps.core.models import FeatureFlag

        FeatureFlag.objects.create(
            key="jobs",
            is_enabled=False,
            expires_on=timezone.localdate() + timedelta(days=7),
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["features"]["jobs"])
