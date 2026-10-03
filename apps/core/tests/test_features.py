"""Feature flags: code defaults, runtime overrides, expiry and rollout."""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_staff, create_user
from apps.core import features
from apps.core.models import FeatureFlag


def make_flag(key="interview", **overrides):
    fields = {
        "key": key,
        "is_enabled": True,
        "rollout_percent": 100,
        "expires_on": timezone.localdate() + timedelta(days=30),
    }
    fields.update(overrides)
    flag = FeatureFlag.objects.create(**fields)
    features.invalidate_cache()
    return flag


class FeatureResolutionTests(TestCase):
    """Flag resolution, using flags whose defaults are what the test needs.

    `career_tools` is the default-off example rather than `interview`: P16
    shipped interviews and turned that flag on, and a test that hardcoded a
    default which later changed was asserting a fact about the roadmap rather
    than about resolution. If `career_tools` ever ships, the same substitution
    is the fix — and the comment is here so the next person makes it in a second.
    """

    def setUp(self):
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

    def test_code_defaults_apply_with_no_rows(self):
        resolved = features.get_features()
        self.assertTrue(resolved["resume"])
        self.assertFalse(resolved["career_tools"])

    def test_a_row_overrides_the_code_default(self):
        make_flag("career_tools", is_enabled=True)
        self.assertTrue(features.is_enabled("career_tools"))

    def test_a_row_can_switch_a_shipped_module_off(self):
        # The incident case: turn a module off without a deploy.
        make_flag("resume", is_enabled=False)
        self.assertFalse(features.is_enabled("resume"))

    def test_an_expired_flag_is_ignored(self):
        # A forgotten flag decays back to the code default rather than
        # governing the product forever.
        make_flag("career_tools", expires_on=timezone.localdate() - timedelta(days=1))
        self.assertFalse(features.is_enabled("career_tools"))

    def test_settings_override_applies_beneath_rows(self):
        with self.settings(CAREERAI_FEATURES={"career_tools": True}):
            self.assertTrue(features.is_enabled("career_tools"))

    def test_unknown_key_is_false(self):
        self.assertFalse(features.is_enabled("teleportation"))


class RolloutTests(TestCase):
    def setUp(self):
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

    def test_staff_only_flag_hides_from_ordinary_users(self):
        make_flag("interview", staff_only=True)

        self.assertFalse(features.is_enabled("interview", create_user()))
        self.assertTrue(features.is_enabled("interview", create_staff()))

    def test_zero_percent_rollout_is_off_for_everyone(self):
        make_flag("interview", rollout_percent=0)
        self.assertFalse(features.is_enabled("interview", create_user()))

    def test_full_rollout_is_on_for_anonymous_visitors(self):
        make_flag("interview", rollout_percent=100)
        self.assertTrue(features.is_enabled("interview", None))

    def test_partial_rollout_is_off_for_anonymous_visitors(self):
        # No stable identity means no stable bucket; flickering a feature per
        # request is worse than not showing it.
        make_flag("interview", rollout_percent=50)
        self.assertFalse(features.is_enabled("interview", None))

    def test_bucket_is_stable_for_a_user(self):
        make_flag("interview", rollout_percent=50)
        user = create_user()

        first = features.is_enabled("interview", user)
        features.invalidate_cache()
        second = features.is_enabled("interview", user)

        self.assertEqual(first, second)

    def test_bucket_differs_between_flags_for_one_user(self):
        # Hashing on the pair stops one user sitting in the first decile of
        # every rollout the platform ever runs.
        user = create_user()
        buckets = {features._bucket(key, user) for key in ("a", "b", "c", "d", "e")}
        self.assertGreater(len(buckets), 1)

    def test_rollout_reaches_roughly_the_configured_share(self):
        users = [create_user() for _ in range(200)]
        make_flag("interview", rollout_percent=50)

        enabled = sum(1 for user in users if features.is_enabled("interview", user))

        self.assertGreater(enabled, 60)
        self.assertLess(enabled, 140)

    def test_staff_see_a_partial_rollout(self):
        make_flag("interview", rollout_percent=1)
        self.assertTrue(features.is_enabled("interview", create_staff()))


class CacheTests(TestCase):
    def setUp(self):
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

    def test_flag_rows_are_cached_between_calls(self):
        make_flag("interview", is_enabled=True)
        features.get_features()

        FeatureFlag.objects.filter(key="interview").update(is_enabled=False)

        self.assertTrue(features.is_enabled("interview"))  # served from cache

    def test_invalidating_the_cache_applies_a_change_immediately(self):
        make_flag("interview", is_enabled=True)
        features.get_features()

        FeatureFlag.objects.filter(key="interview").update(is_enabled=False)
        features.invalidate_cache()

        self.assertFalse(features.is_enabled("interview"))


class NavigationIntegrationTests(TestCase):
    """A flag must reach the navigation, or the UI links to a dead page."""

    def setUp(self):
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

    def test_navigation_hides_a_module_switched_off_at_runtime(self):
        from apps.core.navigation import primary_nav

        make_flag("jobs", is_enabled=False)
        labels = _labels(primary_nav(features.get_features()))

        self.assertNotIn("jobs", " ".join(labels).lower())

    def test_navigation_shows_a_module_switched_on_at_runtime(self):
        from apps.core.navigation import rail_nav

        make_flag("interview", is_enabled=True)
        resolved = features.get_features()

        self.assertTrue(resolved["interview"])
        self.assertIsInstance(rail_nav("candidate", resolved), list)


def _labels(nav) -> list[str]:
    out = []
    for item in nav:
        out.append(item["label"])
        out.extend(child["label"] for child in item.get("children", []))
    return out
