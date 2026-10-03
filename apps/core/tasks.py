"""
Platform housekeeping (SRS §17, §5.14).

One job: the weekly expired-flag report. `FeatureFlag.expires_on` is mandatory
because a flag that outlives its feature is a permanent dead branch — a
condition in the code that nobody remembers the reason for and nobody dares
remove. The expiry date only means something if somebody is told when it
passes, and this is the telling.

It reports; it does not delete. A flag past its date might be a feature that
shipped and needs its branch removed, or one whose rollout genuinely slipped —
and deleting the second on a schedule would switch a live feature off overnight.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="default")
def report_expired_flags() -> list[str]:
    """Names of flags past their expiry date. Logged and returned.

    Expired flags are already ignored at read time — `get_features` falls back
    to the code default — so this is about the dead branch in the source, not
    about behaviour drifting.
    """
    from .models import FeatureFlag

    expired = list(FeatureFlag.objects.expired().values_list("key", flat=True))
    if expired:
        logger.warning(
            "feature flags past their expiry date",
            extra={"flags": expired, "count": len(expired)},
        )
    return expired
