"""
Deployment checks for interviews.

Production had no video provider configured at all, so every environment fell
back to the fake provider — whose join links point at `example.test`. Every
live interview booked in production would have produced a link to nowhere, and
nothing would have said so until two people tried to use it.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, register

FAKE = "apps.interviews.providers.fake.FakeRoomProvider"
JITSI = "apps.interviews.providers.jitsi.JitsiRoomProvider"


@register(Tags.security, deploy=True)
def video_provider_is_real(app_configs=None, **kwargs):
    from apps.core.features import DEFAULT_FEATURES

    if settings.DEBUG or not DEFAULT_FEATURES.get("interview"):
        return []

    path = getattr(settings, "VIDEO_ROOM_PROVIDER", FAKE)
    problems = []
    if path == FAKE:
        problems.append(
            Error(
                "Live interviews are enabled but VIDEO_ROOM_PROVIDER is the fake provider.",
                hint="Set VIDEO_ROOM_PROVIDER to the Jitsi provider and configure JaaS (DEPLOYMENT.md).",
                id="interviews.E001",
            )
        )
    elif path == JITSI:
        options = getattr(settings, "VIDEO_ROOM_OPTIONS", {}) or {}
        missing = [
            name
            for name, present in (
                ("JAAS_APP_ID", options.get("app_id")),
                ("JAAS_API_KEY_ID", options.get("api_key_id")),
                (
                    "JAAS_PRIVATE_KEY or JAAS_PRIVATE_KEY_PATH",
                    options.get("private_key") or options.get("private_key_path"),
                ),
            )
            if not present
        ]
        if missing:
            problems.append(
                Error(
                    "The Jitsi provider is selected but JaaS is not fully configured: "
                    + ", ".join(missing),
                    hint="Without these no signed join link can be made, and live interviews fail at the door.",
                    id="interviews.E002",
                )
            )
    return problems
