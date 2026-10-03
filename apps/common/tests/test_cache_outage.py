"""
Signed-in pages during a Redis outage (SRS §17.4, NFR-4, OPS-01).

Found by stopping Redis under the running app: every signed-in page, including
the operations board whose "Cache and sessions" row exists to report that
outage, returned a 500 after a 38-second hang. Two readers raised — the session
back-fill in Django's `cached_db` store, and the feature-flag cache read by a
context processor on every page — and no Redis call had a timeout.

A cache backend that raises on every call stands in for the stopped server.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.core.cache.backends.base import BaseCache
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import StaffRole
from apps.candidates.tests.factories import create_profile


class UnreachableCache(BaseCache):
    """Every call fails the way a stopped Redis does."""

    def __init__(self, location, params):
        super().__init__(params)

    def _down(self, *args, **kwargs):
        raise ConnectionError("Error 10061 connecting to 127.0.0.1:6380.")

    get = set = add = delete = touch = has_key = clear = get_many = set_many = _down
    delete_many = incr = decr = _down

    def __contains__(self, key):
        self._down()


class SettingsTests(TestCase):
    def test_the_project_session_store_is_the_outage_tolerant_one(self):
        self.assertEqual(settings.SESSION_ENGINE, "apps.accounts.sessions")

    def test_every_redis_cache_gives_up_within_a_second(self):
        from config.settings import base

        for alias, cache in base.CACHES.items():
            with self.subTest(cache=alias):
                self.assertLessEqual(cache["OPTIONS"]["socket_connect_timeout"], 1)
                self.assertLessEqual(cache["OPTIONS"]["socket_timeout"], 1)


class SessionOutageTests(TestCase):
    def setUp(self):
        self.support = create_profile().user
        StaffRole.objects.create(user=self.support, level="support")

    def test_a_signed_in_person_stays_signed_in_when_the_cache_is_down(self):
        from apps.common import health
        from apps.core import features, selectors

        # Signed in while Redis was up: the session row is in the database.
        self.client.force_login(self.support)
        down = UnreachableCache("", {})
        broker_down = {"available": False, "error": "OperationalError", "depths": {}, "total": 0}

        with (
            mock.patch("django.contrib.sessions.backends.cached_db.caches", {"sessions": down}),
            mock.patch.object(features, "cache", down),
            mock.patch.object(health, "cache", down),
            mock.patch.object(selectors, "queue_depths", return_value=broker_down),
        ):
            response = self.client.get(reverse("staff:operations"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["user"], self.support)
        self.assertContains(response, "error: ConnectionError")
        self.assertContains(response, "The broker could not be reached")

    def test_signing_out_during_an_outage_is_not_silently_half_done(self):
        # The cached copy of a deleted session would be served again when
        # Redis came back, so the store must not swallow a failed delete.
        from django.contrib.sessions.backends.cached_db import SessionStore as Django

        from apps.accounts.sessions import SessionStore

        self.assertIs(SessionStore.delete, Django.delete)


class FeatureCacheOutageTests(TestCase):
    def test_flags_are_read_from_the_database_when_the_cache_raises(self):
        import datetime as dt

        from django.utils import timezone

        from apps.core import features
        from apps.core.models import FeatureFlag

        FeatureFlag.objects.create(
            key="messaging",
            is_enabled=False,
            expires_on=timezone.localdate() + dt.timedelta(days=10),
        )
        broken = mock.Mock()
        broken.get.side_effect = ConnectionError("down")
        broken.set.side_effect = ConnectionError("down")

        with mock.patch.object(features, "cache", broken):
            self.assertFalse(features.get_features()["messaging"])

    def test_invalidating_during_an_outage_does_not_undo_the_saved_change(self):
        from apps.core import features

        broken = mock.Mock()
        broken.delete.side_effect = ConnectionError("down")

        with mock.patch.object(features, "cache", broken):
            features.invalidate_cache()  # must not raise
