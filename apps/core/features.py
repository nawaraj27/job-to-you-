"""
Feature registry.

Product rule for this platform: never advertise a surface that does not work
yet. Rather than scattering `{% if %}` guards through templates, every module
is declared here and the navigation / homepage builders filter on it.

Three layers, most specific last:

  1. DEFAULT_FEATURES   what the code actually ships. The honest baseline.
  2. settings override  CAREERAI_FEATURES, for an environment-wide decision.
  3. FeatureFlag rows   runtime control: incident switch-off, staged rollout,
                        staff-only preview. Expired flags are ignored, so a
                        forgotten flag decays back to the code default instead
                        of silently governing the product forever.

The database layer is cached: this runs in a context processor on every
request, and a per-request query for a value that changes once a month is not
a trade worth making.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

DEFAULT_FEATURES: dict[str, bool] = {
    "resume": True,  # upload, analysis, builders, templates
    "jobs": True,  # browse, search, detail, saved searches and alerts
    "job_matching": True,  # roles scored against a CV (P13)
    "applications": True,  # applying, the pipeline, offers
    "employers": True,  # employer portal
    "messaging": True,  # conversations between a company and a candidate
    "dashboard": True,  # signed-in candidate overview
    # The AI kill switch (ADR-A28). The one piece of AI configuration that is
    # runtime rather than code, because switching AI off during an incident
    # must not require a deploy.
    "ai": True,  # the AI run pipeline
    "interview": True,  # practice, AI text interviews and scheduling (P16)
    # Cover letters shipped in P14 and are reached under the `applications`
    # flag with the rest of the application-time tools. What is left behind this
    # one is the career roadmap, which is not built (SRS §21).
    "career_tools": False,  # career roadmap — not built
    # Plans, entitlements and the billing screens (P18). On, because the
    # pricing page has been advertising these limits since P0 and INV-6 cuts
    # both ways: a limit that is advertised and not enforced is the same defect
    # as a feature that is linked to and not built.
    "billing": True,
    # Demonstration sponsored slots (`core.ads`): fictional advertisers, no
    # tracking, three fixed places. Off here turns every slot off at once.
    "demo_ads": True,
    "pricing": True,
    "about": True,
    "accounts": True,
}

CACHE_KEY = "core:feature-flags"
CACHE_SECONDS = 30


def _flag_rows() -> dict[str, dict]:
    """Active flag rows as plain dicts, cached.

    Plain dicts rather than model instances so the cached value stays valid
    across a deploy that changes the model.
    """
    # The flag set is read by a context processor on every page, so a cache
    # outage here was an outage of every page. The rows are in the database;
    # uncached is slower, not wrong (OPS-01).
    try:
        cached = cache.get(CACHE_KEY)
    except Exception:  # noqa: BLE001 - an unreachable cache is a miss
        logger.warning("feature flag cache unavailable; read from the database")
        cached = None
    if cached is not None:
        return cached

    from .models import FeatureFlag

    rows = {
        flag.key: {
            "is_enabled": flag.is_enabled,
            "rollout_percent": flag.rollout_percent,
            "staff_only": flag.staff_only,
        }
        for flag in FeatureFlag.objects.active()
    }
    with contextlib.suppress(Exception):
        cache.set(CACHE_KEY, rows, CACHE_SECONDS)
    return rows


def invalidate_cache() -> None:
    """Called after a flag is written, so an incident switch takes effect now.

    Failing to delete is not a reason to fail a flag change that has already
    been saved — which would also skip that change's audit entry. The worst
    case is a Redis that is answering slowly but still holds the old set, and
    that copy expires within `CACHE_SECONDS`.
    """
    try:
        cache.delete(CACHE_KEY)
    except Exception:  # noqa: BLE001
        logger.warning("feature flag cache unavailable; nothing to invalidate")


def get_features(user=None) -> dict[str, bool]:
    """Effective flags for this viewer."""
    features = {**DEFAULT_FEATURES, **(getattr(settings, "CAREERAI_FEATURES", {}) or {})}

    for key, row in _flag_rows().items():
        features[key] = _resolve(key, row, user)

    return features


def is_enabled(name: str, user=None) -> bool:
    """True when the named module is shipped and safe to link to."""
    return bool(get_features(user).get(name, False))


def _resolve(key: str, row: dict, user) -> bool:
    if not row["is_enabled"]:
        return False

    is_staff = bool(user is not None and getattr(user, "is_authenticated", False) and user.is_staff)
    if row["staff_only"]:
        return is_staff
    if is_staff:
        return True

    percent = row["rollout_percent"]
    if percent >= 100:
        return True
    if percent <= 0:
        return False

    return _bucket(key, user) < percent


def _bucket(key: str, user) -> int:
    """Stable 0-99 bucket for a user and flag.

    Hashed on the pair so one user is not in the first decile of every rollout,
    and stable so a user does not see a feature appear and disappear between
    requests. An anonymous viewer has no stable identity, so a partial rollout
    is treated as off for them rather than flickering.
    """
    user_id = getattr(user, "pk", None) if getattr(user, "is_authenticated", False) else None
    if user_id is None:
        return 100

    digest = hashlib.sha256(f"{key}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100
