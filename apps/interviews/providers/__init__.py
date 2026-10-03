"""
Choosing a video-room provider.

Configuration in settings rather than a database row, for the same reason AI
routing is code (ADR-A28): swapping a video vendor is a deploy, and a runtime
switch would be a control nobody uses and everybody has to reason about.
"""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from .base import (
    Room,
    RoomProviderError,
    VideoRoomProvider,
    constant_time_matches,
    hash_token,
    new_token,
)

__all__ = [
    "Room",
    "RoomProviderError",
    "VideoRoomProvider",
    "constant_time_matches",
    "hash_token",
    "new_token",
    "room_provider",
]

#: The provider when settings name none. The fake, deliberately: a development
#: machine with no JaaS credentials should schedule interviews that work
#: end-to-end rather than fail at the last step, and an operator who has not
#: configured video has not enabled the live-interview flag either.
DEFAULT_PROVIDER = "apps.interviews.providers.fake.FakeRoomProvider"


def room_provider() -> VideoRoomProvider:
    path = getattr(settings, "VIDEO_ROOM_PROVIDER", DEFAULT_PROVIDER)
    options = getattr(settings, "VIDEO_ROOM_OPTIONS", {})
    return import_string(path)(**options)
