"""
Short-lived, single-use access tokens for private files (SRS §14.1).

A private file has no durable URL. To read one you need a token that names the
file, the reader and the purpose, expires in minutes, and works exactly once.

Single use is enforced by a durable ledger in PostgreSQL, not by the cache
(ADR-A38). A cache entry can be lost to eviction, to `cache.clear()` and to a
Redis restart, and losing it re-arms the token — which for a private CV is a
confidentiality failure. The ledger fails closed instead: no database, no
download, so no replay window during an outage.

`django.core.signing` provides the cryptography (HMAC over the serialised
payload, keyed by SECRET_KEY with a namespace salt). Nothing here rolls its own.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core import signing
from django.utils import timezone

from apps.common import tokens
from apps.common.errors import NotAllowed
from apps.common.uuid7 import uuid7

SALT = "apps.files.access"


def issue(
    attachment,
    *,
    actor_id: int | None = None,
    purpose: str = "",
    max_age: int | None = None,
) -> str:
    """Mint a token for one read of one attachment."""
    payload = {
        "aid": str(attachment.public_id),
        "act": actor_id,
        "pur": purpose[:64],
        "jti": str(uuid7()),
    }
    return signing.dumps(payload, salt=SALT, compress=True)


def access_url(attachment, *, actor_id: int | None, purpose: str) -> str:
    """A one-read link to `files:access` for this attachment.

    Minting a token proves nothing on its own: the file view still runs the
    access policy for whoever follows the link.
    """
    from django.urls import reverse

    token = issue(attachment, actor_id=actor_id, purpose=purpose)
    return f"{reverse('files:access', args=[attachment.public_id])}?t={token}"


def verify(token: str, *, expected_public_id: str, max_age: int | None = None) -> dict[str, Any]:
    """Validate a token and burn it.

    Raises NotAllowed for anything wrong — expired, tampered with, issued for a
    different file, or already used. The caller must not distinguish these in
    its response: telling an attacker which of the four failed is free
    information.
    """
    ttl = max_age if max_age is not None else settings.FILES_SIGNED_URL_MAX_AGE

    try:
        payload = signing.loads(token, salt=SALT, max_age=ttl)
    except signing.SignatureExpired:
        raise NotAllowed("This link has expired. Open the file again to get a new one.") from None
    except signing.BadSignature:
        raise NotAllowed("This link is not valid.") from None

    if payload.get("aid") != str(expected_public_id):
        raise NotAllowed("This link is not valid.")

    # A unique constraint decides the race: two simultaneous requests both
    # attempt the insert and exactly one succeeds.
    first_use = tokens.consume(
        jti=payload["jti"],
        purpose=tokens.Purpose.FILE_ACCESS,
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )
    if not first_use:
        raise NotAllowed("This link has already been used. Open the file again.")

    return payload
