"""
Jitsi via JaaS — Jitsi as a Service, 8x8 (SRS §11.3).

**Every join URL carries a signed token.** The first version returned the bare
room URL, reasoning that an unguessable name was protection enough. It is not:
the URL is a bearer credential that works for anybody it is forwarded to, for
ever, and carries no identity — the interviewer could not tell the candidate
from whoever the candidate pasted the link to.

A JaaS JWT fixes each of those, and is what JaaS itself enforces at the door
once the tenant requires authentication:

  * **RS256**, signed with the tenant's API key; `kid` names the key;
  * `aud="jitsi"`, `iss="chat"`, `sub=<AppID>` — the fixed JaaS values;
  * `room` is **this** room, not the `*` wildcard, so a token for one interview
    opens no other;
  * `nbf`/`exp` bound it to the interview's join window;
  * `context.user` names who is joining; `moderator` is `"true"` only for the
    interviewer;
  * `context.features` switches off recording, livestreaming and transcription:
    the product does not store interview video, and the room should not offer
    to.

Without a private key configured this provider **refuses** to build a URL,
rather than returning an unsigned one. A link that works without a token is the
defect this module exists to remove.

Required configuration (see docs/DEPLOYMENT.md): `JAAS_APP_ID`,
`JAAS_API_KEY_ID` (the `kid`, "vpaas-magic-cookie-.../abc123"), and the private
key as `JAAS_PRIVATE_KEY` (PEM) or `JAAS_PRIVATE_KEY_PATH`. In the JaaS console,
authentication must be required so a URL without a token is refused.
"""

from __future__ import annotations

import datetime as dt
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.utils import timezone

from .base import Room, RoomProviderError, hash_token, new_token

#: Clock skew allowed on `nbf`. JaaS servers and ours will not agree to the second.
SKEW = dt.timedelta(seconds=60)

#: The longest a token may live when the caller gives no expiry.
DEFAULT_TTL = dt.timedelta(hours=2)

#: Features the product does not use, and does not want a participant to switch on.
FEATURES = {
    "livestreaming": False,
    "recording": False,
    "transcription": False,
    "outbound-call": False,
    "sip-outbound-call": False,
    "sip-inbound-call": False,
    "inbound-call": False,
    "file-upload": False,
    "list-visitors": False,
    "create-polls": False,
    "send-groupchat": True,
}


class JitsiRoomProvider:
    key = "jitsi"

    def __init__(
        self,
        *,
        app_id: str = "",
        api_key_id: str = "",
        private_key: str = "",
        private_key_path: str = "",
        base_url: str = "https://8x8.vc",
        **_: Any,
    ) -> None:
        self.app_id = app_id
        self.api_key_id = api_key_id
        self.base_url = base_url.rstrip("/")
        self._private_key = private_key.replace("\\n", "\n") if private_key else ""
        self._private_key_path = private_key_path

    # -- rooms --------------------------------------------------------------

    def create_room(self, *, subject: str, starts_at: Any, ends_at: Any) -> Room:
        # The subject is deliberately not in the room name: the name travels in
        # URLs, logs and browser history, and says nothing about who is inside.
        reference = f"careerai-{secrets.token_urlsafe(18)}"
        token = new_token()
        return Room(
            provider=self.key,
            room_ref=reference,
            token=token,
            token_hash=hash_token(token),
            join_url=f"{self.base_url}/{self.app_id}/{reference}",
        )

    def join_url_for(
        self,
        *,
        room_ref: str,
        display_name: str,
        is_host: bool,
        user_id: str = "",
        email: str = "",
        expires_at: dt.datetime | None = None,
    ) -> str:
        token = self.token_for(
            room_ref=room_ref,
            display_name=display_name,
            is_host=is_host,
            user_id=user_id,
            email=email,
            expires_at=expires_at,
        )
        return f"{self.base_url}/{quote(self.app_id)}/{quote(room_ref)}?jwt={token}"

    # -- tokens -------------------------------------------------------------

    def token_for(
        self,
        *,
        room_ref: str,
        display_name: str,
        is_host: bool,
        user_id: str = "",
        email: str = "",
        expires_at: dt.datetime | None = None,
        now: dt.datetime | None = None,
    ) -> str:
        import jwt

        if not (self.app_id and self.api_key_id):
            raise RoomProviderError(
                "JaaS is not configured: JAAS_APP_ID and JAAS_API_KEY_ID are required."
            )
        key = self._key()
        now = now or timezone.now()
        expires = expires_at or (now + DEFAULT_TTL)
        if expires <= now:
            raise RoomProviderError("That room has closed.")

        payload = {
            "aud": "jitsi",
            "iss": "chat",
            "sub": self.app_id,
            "room": room_ref,
            "nbf": int((now - SKEW).timestamp()),
            "exp": int(expires.timestamp()),
            "context": {
                "user": {
                    "id": user_id,
                    "name": display_name or "Participant",
                    "email": email,
                    "avatar": "",
                    "moderator": "true" if is_host else "false",
                },
                "features": FEATURES,
                "room": {"regex": False},
            },
        }
        return jwt.encode(
            payload, key, algorithm="RS256", headers={"kid": self.api_key_id, "typ": "JWT"}
        )

    def _key(self) -> str:
        if self._private_key:
            return self._private_key
        if self._private_key_path:
            try:
                return Path(self._private_key_path).read_text(encoding="utf-8")
            except OSError as exc:
                raise RoomProviderError("The JaaS private key file could not be read.") from exc
        raise RoomProviderError(
            "JaaS is not configured with a private key, so no signed join link can be made."
        )
