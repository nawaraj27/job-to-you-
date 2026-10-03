"""
Rate limits on the search endpoints (SRS §15.4, ADR-A41).

Deferred out of P15 with a reason — what talent search defends against is a
paying company reading too much, which anomaly detection measures per day, and
a per-minute limit would not have caught it. That is still true. What the
hardening pass adds is the other half: the endpoints are expensive and one of
them is public, and §15.4 lists search among the limited surfaces.

The two rules are deliberately different, and both directions are asserted:

  * **Job search fails open.** It is public and it is the front door; taking it
    down because a cache is missing would be self-inflicted damage.
  * **Talent search fails closed and is keyed per membership.** Per company
    would let one compromised seat spend the team's allowance; per IP would
    punish an office.
"""

from __future__ import annotations

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.candidates.services import talent
from apps.candidates.services.talent import TalentQuery
from apps.candidates.tests.factories import create_profile
from apps.common import ratelimit
from apps.companies.permissions import Grant, Role
from apps.companies.tests.factories import add_member, company_with_owner


class JobSearchLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        cache.clear()
        self.url = reverse("jobs:list")

    def tearDown(self):
        cache.clear()

    def test_ordinary_browsing_is_never_limited(self):
        for _ in range(5):
            self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_rule_is_applied_to_the_endpoint(self):
        # Spend the allowance directly, then assert the view refuses — which
        # proves the view consults the same rule rather than a copy of it.
        for _ in range(ratelimit.JOB_SEARCH_PER_CLIENT.limit + 1):
            try:
                ratelimit.check(ratelimit.JOB_SEARCH_PER_CLIENT, "ip:127.0.0.1")
            except ratelimit.RateLimited:
                break

        self.assertEqual(self.client.get(self.url).status_code, 429)

    def test_a_signed_in_visitor_is_counted_as_themselves(self):
        # A shared office address must not have one person's browsing count
        # against everybody else's.
        profile = create_profile()
        self.client.force_login(profile.user)

        for _ in range(ratelimit.JOB_SEARCH_PER_CLIENT.limit + 1):
            try:
                ratelimit.check(ratelimit.JOB_SEARCH_PER_CLIENT, "ip:127.0.0.1")
            except ratelimit.RateLimited:
                break

        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_rule_fails_open(self):
        # Public search staying up through a cache outage is the deliberate
        # trade (ADR-A41).
        self.assertFalse(ratelimit.JOB_SEARCH_PER_CLIENT.fail_closed)


class TalentSearchLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        cache.clear()
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.query = TalentQuery.from_mapping({"q": "django"})

    def tearDown(self):
        cache.clear()

    def test_a_search_within_the_limit_runs(self):
        talent.search(membership=self.membership, actor=self.owner, query=self.query)

    def test_the_limit_refuses_the_next_one(self):
        # One fixed window for the whole test: the limiter buckets by
        # time // 60, and a slow run that crossed a minute boundary between
        # the 30th and the 31st search started a fresh window and passed.
        from unittest import mock

        with mock.patch("apps.common.ratelimit.time.time", return_value=1_800_000_000.0):
            for _ in range(ratelimit.CANDIDATE_SEARCH_PER_MEMBERSHIP.limit):
                talent.search(membership=self.membership, actor=self.owner, query=self.query)

            with self.assertRaises(ratelimit.RateLimited):
                talent.search(membership=self.membership, actor=self.owner, query=self.query)

    def test_one_seat_does_not_spend_anothers_allowance(self):
        colleague = add_member(self.company, create_profile().user, role=Role.RECRUITER)
        self.assertIn(Grant.CANDIDATE_SEARCH, colleague.grants)

        for _ in range(ratelimit.CANDIDATE_SEARCH_PER_MEMBERSHIP.limit):
            talent.search(membership=self.membership, actor=self.owner, query=self.query)

        # The colleague's own allowance is untouched.
        talent.search(membership=colleague, actor=colleague.user, query=self.query)

    def test_the_rule_fails_closed(self):
        self.assertTrue(ratelimit.CANDIDATE_SEARCH_PER_MEMBERSHIP.fail_closed)

    def test_the_limit_is_spent_only_by_a_search_that_ran(self):
        # An empty query is refused before the limiter, so hammering the form
        # with nothing in it cannot lock somebody out of their own tool.
        from apps.common.errors import InvalidState

        empty = TalentQuery.from_mapping({})
        for _ in range(ratelimit.CANDIDATE_SEARCH_PER_MEMBERSHIP.limit + 5):
            with self.assertRaises(InvalidState):
                talent.search(membership=self.membership, actor=self.owner, query=empty)

        talent.search(membership=self.membership, actor=self.owner, query=self.query)
