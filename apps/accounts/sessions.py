"""
Sessions that survive a cache outage (SRS §17.4, NFR-4, OPS-01).

`config/settings/base.py` chose `cached_db` so the database is the durable copy
of every session and Redis only a read-through cache. Django's backend keeps
that promise on the read (`cache.get` failures are swallowed) and on the save,
but not on the back-fill after a database read: when Redis is down, `load()`
reads the session from PostgreSQL and then raises trying to put it back in the
cache. Every signed-in page returned a 500 — including the operations board,
whose "Cache and sessions" row exists to report exactly that outage.

This store finishes the job the setting describes for *reading*: a cache that
cannot be back-filled is logged, and the session carries on from the database.

**Only reading.** Deleting is left to Django, which raises when the cache is
down, and that is deliberate: a sign-out that removed the database row but left
the cached copy would come back to life the moment Redis returned, because a
cache hit is served without consulting the database. A sign-out that fails
loudly during an outage is the safe failure; one that silently half-happens is
not.
"""

from __future__ import annotations

import logging

from django.contrib.sessions.backends.cached_db import SessionStore as CachedDBStore

logger = logging.getLogger("django.contrib.sessions")


class SessionStore(CachedDBStore):
    def load(self):
        try:
            data = self._cache.get(self.cache_key)
        except Exception:  # noqa: BLE001 - an unreachable cache is a miss
            data = None

        if data is not None:
            return data

        stored = self._get_session_from_db()
        if not stored:
            return {}

        data = self.decode(stored.session_data)
        try:
            self._cache.set(self.cache_key, data, self.get_expiry_age(expiry=stored.expire_date))
        except Exception:  # noqa: BLE001 - the database copy is the session
            logger.warning("session cache unavailable; served from the database")
        return data
