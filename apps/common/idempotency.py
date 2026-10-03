"""
Idempotency helper (SRS §8.1, DEVELOPER.md §3.8).

Any endpoint that creates a record accepts an Idempotency-Key. The guarantee:
replaying the same key with the same request produces the same outcome and does
not create a second record.

Three distinct situations must be told apart, because collapsing them produces
either duplicate records or silent data loss:

  1. First use            -> claim it, run the operation, record the outcome.
  2. Replay, completed    -> return the stored outcome. No second write.
  3. Replay, in flight    -> Conflict. The first attempt has not finished; the
                             caller must retry, not proceed.

A fourth case is a caller error: the same key with a *different* request body.
That is always a bug or an attack, and it raises rather than guessing which
request was meant.

The model lives in apps.accounts (SRS §5.2 places the table there); the
mechanism lives here because it is infrastructure, used by every domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .errors import Conflict

if TYPE_CHECKING:
    from apps.accounts.models import User
from .hashing import stable_hash


@dataclass(frozen=True)
class Claim:
    """Outcome of attempting to claim a key."""

    replayed: bool
    response: dict[str, Any] | None = None


def fingerprint(payload: Any) -> str:
    """Stable digest of a request body, used to detect key reuse."""
    return stable_hash(payload)


def claim(
    *,
    key: str,
    endpoint: str,
    request_hash: str,
    user: User | None = None,
    ttl_seconds: int | None = None,
) -> Claim:
    """Claim an idempotency key, or report that it has already been used.

    Must be called outside the operation's own transaction, so that the claim
    survives a rollback of the work it guards — otherwise a failed attempt
    would release its key and a retry could double-write.
    """
    from apps.accounts.models import IdempotencyKey

    ttl = ttl_seconds if ttl_seconds is not None else settings.IDEMPOTENCY_TTL_SECONDS
    now = timezone.now()

    existing = IdempotencyKey.objects.filter(key=key, endpoint=endpoint).first()

    if existing is not None and existing.expires_at <= now:
        # Expired: the window in which a replay is meaningful has passed.
        existing.delete()
        existing = None

    if existing is not None:
        if existing.request_hash != request_hash:
            raise Conflict(
                "This idempotency key was already used for a different request.",
                code="idempotency_key_reused",
            )
        if existing.response_ref is None:
            raise Conflict(
                "An identical request is still being processed. Try again shortly.",
                code="idempotency_in_flight",
            )
        return Claim(replayed=True, response=existing.response_ref)

    try:
        with transaction.atomic():
            IdempotencyKey.objects.create(
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                user=user,
                expires_at=now + timedelta(seconds=ttl),
            )
    except IntegrityError:
        # Another request claimed it between the read and the write.
        raise Conflict(
            "An identical request is still being processed. Try again shortly.",
            code="idempotency_in_flight",
        ) from None

    return Claim(replayed=False)


def record(*, key: str, endpoint: str, response: dict[str, Any]) -> None:
    """Store the outcome so a later replay can return it."""
    from apps.accounts.models import IdempotencyKey

    IdempotencyKey.objects.filter(key=key, endpoint=endpoint).update(response_ref=response)


def release(*, key: str, endpoint: str) -> None:
    """Drop a claim whose operation failed and should be retryable immediately.

    Used only when the failure is known not to have written anything — a
    validation error, for example. When in doubt, leave the claim in place and
    let it expire: a blocked retry is recoverable, a duplicate record is not.
    """
    from apps.accounts.models import IdempotencyKey

    IdempotencyKey.objects.filter(key=key, endpoint=endpoint, response_ref__isnull=True).delete()


def purge_expired() -> int:
    """Delete expired keys. Called by the scheduled cleanup command."""
    from apps.accounts.models import IdempotencyKey

    deleted, _ = IdempotencyKey.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted
