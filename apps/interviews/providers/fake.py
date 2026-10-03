"""
A deterministic room provider, for tests and for development.

A real implementation of the real protocol rather than a mock scattered through
test files: what is under test is scheduling — holds, bookings, the exclusion
constraint, cancellation — and every one of those is provider-agnostic. Testing
them against a patched HTTP client would be testing the patch.
"""

from __future__ import annotations

import itertools
from typing import Any

from .base import Room, hash_token, new_token

_counter = itertools.count(1)


class FakeRoomProvider:
    key = "fake"

    def __init__(self, **_: Any) -> None:
        self.created: list[str] = []

    def create_room(self, *, subject: str, starts_at: Any, ends_at: Any) -> Room:
        reference = f"fake-room-{next(_counter)}"
        token = new_token()
        self.created.append(reference)
        return Room(
            provider=self.key,
            room_ref=reference,
            token=token,
            token_hash=hash_token(token),
            join_url=f"https://example.test/{reference}",
        )

    def join_url_for(self, *, room_ref: str, display_name: str, is_host: bool, **_: Any) -> str:
        role = "host" if is_host else "guest"
        return f"https://example.test/{room_ref}?role={role}"
