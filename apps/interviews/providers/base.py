"""
The video-room seam (SRS §11.3).

One protocol, two implementations, and **no service ever touches a vendor**.
Jitsi via JaaS is what v1 ships; the interface exists so self-hosting later is
configuration rather than a rewrite, and so the test suite never needs a network.

The same shape as the AI provider seam (ADR-A28) for the same reason: what
varies between vendors — how a room is named, how a token is minted, how long it
lives — stops at this boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Room:
    """A room somebody can be let into.

    `token` is returned once and never stored — `token_hash` is what goes in the
    database. A join token is a credential, and a stored credential is a leak
    waiting for a backup to be read.
    """

    provider: str
    room_ref: str
    token: str
    token_hash: str
    join_url: str


class RoomProviderError(Exception):
    """The provider could not give us a room."""


class VideoRoomProvider(Protocol):
    key: str

    def create_room(self, *, subject: str, starts_at, ends_at) -> Room:
        """Reserve a private room for one interview."""
        ...

    def join_url_for(
        self,
        *,
        room_ref: str,
        display_name: str,
        is_host: bool,
        user_id: str = "",
        email: str = "",
        expires_at: Any = None,
    ) -> str:
        """A short-lived, per-participant URL. Raises RoomProviderError."""
        ...


def hash_token(token: str) -> str:
    """How a join token is stored.

    SHA-256 rather than a password hash: this is a high-entropy random string
    with a short life, not something a person chose, so there is nothing to
    brute-force and no reason to pay for a slow hash on every join.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    """A join token.

    `secrets` because a guessable room token is a stranger walking into an
    interview — §11.3 says exactly that, and it is the whole reason rooms are
    not named after the job.
    """
    return secrets.token_urlsafe(32)


def constant_time_matches(token: str, stored_hash: str) -> bool:
    """Compare a presented token with the stored hash, without a timing leak."""
    return hmac.compare_digest(hash_token(token), stored_hash)
