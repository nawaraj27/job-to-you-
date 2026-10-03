"""
CSP, rate limiting, and the Redis role separation (ADR-A31, A35, A41).

Documentation that claims a security control exists while the repository does
not enforce it is worse than no claim at all, so each control is asserted
through a real response or a real call.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache, caches
from django.core.cache.backends.base import BaseCache
from django.test import TestCase, override_settings

from apps.common import ratelimit
from apps.common.ratelimit import RateLimited, Rule

#: Only the default cache breaks; sessions must keep working, or the
#: failure under test is 'the whole request died', not 'the limiter coped'.
_BROKEN_CACHES = {
    "default": {"BACKEND": "apps.common.tests.test_security_baseline.BrokenCache"},
    "sessions": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "careerai-test-sessions",
    },
}


class ContentSecurityPolicyTests(TestCase):
    def test_policy_is_enforced_not_merely_reported(self):
        response = self.client.get("/")

        self.assertIn("Content-Security-Policy", response)
        self.assertNotIn("Content-Security-Policy-Report-Only", response)

    def test_script_src_has_a_nonce_and_no_unsafe_inline(self):
        # unsafe-inline on script-src would defeat the nonce entirely, which is
        # the control that actually blocks injected resume content.
        policy = self.client.get("/")["Content-Security-Policy"]

        self.assertIn("'nonce-", policy)
        script_src = next(
            part for part in policy.split(";") if part.strip().startswith("script-src")
        )
        self.assertNotIn("unsafe-inline", script_src)

    def test_dangerous_vectors_are_closed(self):
        policy = self.client.get("/")["Content-Security-Policy"]

        self.assertIn("object-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("base-uri 'self'", policy)

    def test_nonce_differs_between_requests(self):
        # A predictable nonce lets an attacker authorise their own script.
        first = self.client.get("/")["Content-Security-Policy"]
        second = self.client.get("/")["Content-Security-Policy"]

        self.assertNotEqual(first, second)

    def test_the_inline_theme_script_carries_the_nonce(self):
        response = self.client.get("/")
        policy = response["Content-Security-Policy"]
        nonce = policy.split("'nonce-")[1].split("'")[0]

        self.assertContains(response, f'<script nonce="{nonce}">')

    @override_settings(CSP_REPORT_ONLY=True)
    def test_report_only_mode_is_available_for_a_rollout(self):
        response = self.client.get("/")

        self.assertIn("Content-Security-Policy-Report-Only", response)
        self.assertNotIn("Content-Security-Policy", response)


class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_requests_within_the_limit_are_allowed(self):
        rule = Rule("test.allow", limit=3, window_seconds=60)

        remaining = [ratelimit.check(rule, "actor") for _ in range(3)]

        self.assertEqual(remaining, [2, 1, 0])

    def test_exceeding_the_limit_raises(self):
        rule = Rule("test.deny", limit=2, window_seconds=60)
        ratelimit.check(rule, "actor")
        ratelimit.check(rule, "actor")

        with self.assertRaises(RateLimited) as caught:
            ratelimit.check(rule, "actor")

        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, 60)

    def test_identities_are_counted_independently(self):
        rule = Rule("test.independent", limit=1, window_seconds=60)
        ratelimit.check(rule, "first")

        ratelimit.check(rule, "second")  # must not raise

    def test_inspection_does_not_consume(self):
        rule = Rule("test.peek", limit=2, window_seconds=60)

        ratelimit.check(rule, "actor", consume=False)
        ratelimit.check(rule, "actor", consume=False)

        self.assertEqual(ratelimit.check(rule, "actor"), 1)

    def test_identifiers_are_hashed_out_of_cache_keys(self):
        # Cache keys are visible in Redis and in monitoring tools; an email
        # address does not belong in either.
        key = ratelimit._key(Rule("test.hash", limit=1, window_seconds=60), "person@example.com")

        self.assertNotIn("person@example.com", key)

    def test_auth_shaped_rules_fail_closed_when_the_cache_is_unavailable(self):
        # Refusing a login attempt during a cache outage is safe. Admitting one
        # is not.
        rule = Rule("test.closed", limit=5, window_seconds=60, fail_closed=True)

        with self.settings(CACHES=_BROKEN_CACHES), self.assertRaises(RateLimited):
            ratelimit.check(rule, "actor")

    def test_browse_shaped_rules_fail_open_when_the_cache_is_unavailable(self):
        # Taking job search down because a cache is missing is self-inflicted.
        rule = Rule("test.open", limit=5, window_seconds=60, fail_closed=False)

        with self.settings(CACHES=_BROKEN_CACHES):
            self.assertEqual(ratelimit.check(rule, "actor"), 5)

    @override_settings(RATELIMIT_ENABLED=False)
    def test_the_kill_switch_disables_enforcement(self):
        rule = Rule("test.off", limit=1, window_seconds=60)

        for _ in range(5):
            ratelimit.check(rule, "actor")

    def test_configured_rules_cover_the_security_sensitive_operations(self):
        for rule in (
            ratelimit.LOGIN_PER_IDENTITY,
            ratelimit.LOGIN_PER_IP,
            ratelimit.SIGNUP_PER_IP,
            ratelimit.PASSWORD_RESET_PER_IDENTITY,
            ratelimit.UPLOAD_PER_USER,
            ratelimit.AI_PER_USER,
            ratelimit.APPLICATION_PER_USER,
            ratelimit.CANDIDATE_SEARCH_PER_MEMBERSHIP,
        ):
            with self.subTest(rule=rule.name):
                self.assertTrue(rule.fail_closed, f"{rule.name} must fail closed")
                self.assertGreater(rule.limit, 0)


class BrokenCache(BaseCache):
    """A cache backend whose operations fail, to exercise the failure policy.

    Subclasses BaseCache rather than being a bare stub: Django's connection
    handler keeps whatever object it is given, so an incomplete double leaks
    out of the override and poisons every later test that touches a cache.
    """

    def get(self, *args, **kwargs):
        raise RuntimeError("cache unavailable")

    def add(self, *args, **kwargs):
        raise RuntimeError("cache unavailable")

    def incr(self, *args, **kwargs):
        raise RuntimeError("cache unavailable")

    def set(self, *args, **kwargs):
        raise RuntimeError("cache unavailable")


class RedisSeparationTests(TestCase):
    def test_sessions_use_their_own_cache_alias(self):
        # One keyspace previously served cache, sessions and the broker, so a
        # routine cache.clear() logged every user out.
        self.assertEqual(settings.SESSION_CACHE_ALIAS, "sessions")
        self.assertIn("sessions", settings.CACHES)

    def test_sessions_are_backed_by_the_database_not_only_the_cache(self):
        # Redis runs without persistence in this deployment, so a cache-only
        # session store would log everyone out on every restart.
        # The project store (OPS-01) is `cached_db` with a read path that
        # survives a Redis outage; what matters here is that it is still that.
        from importlib import import_module

        from django.contrib.sessions.backends.cached_db import SessionStore as CachedDB

        store = import_module(settings.SESSION_ENGINE).SessionStore
        self.assertTrue(issubclass(store, CachedDB))

    def test_clearing_the_application_cache_does_not_touch_sessions(self):
        caches["sessions"].set("session-key", "value", 300)
        cache.set("app-key", "value", 300)

        cache.clear()

        self.assertIsNone(cache.get("app-key"))
        self.assertEqual(caches["sessions"].get("session-key"), "value")
