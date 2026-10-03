"""
Single-use token consumption (ADR-A38).

A signed token proves a link was issued. Making it *single use* is a security
decision, and security decisions belong in the database rather than in a cache.

The previous implementation recorded consumption with `cache.add` against Redis.
Three independent events could erase that record — LRU eviction under memory
pressure, a `cache.clear()` during a routine feature-flag change, and any Redis
restart, because the compose file runs Redis with `--save "" --appendonly no`
and therefore has no persistence at all. Losing the record re-arms a download
token for a private CV.

This ledger fails in the opposite direction: if PostgreSQL is unavailable
nothing is served at all, so a token cannot be replayed during an outage. That
is the correct failure mode for a confidentiality control.

Generic rather than file-specific because the SRS already requires single-use
tokens for password reset, invitations and interview join links.
"""

from __future__ import annotations

from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone


class Purpose:
    """Namespaces for token identifiers, so one purpose cannot burn another."""

    FILE_ACCESS = "file_access"
    PASSWORD_RESET = "password_reset"  # noqa: S105 - a namespace, not a credential
    EMAIL_VERIFICATION = "email_verification"
    COMPANY_INVITE = "company_invite"
    INTERVIEW_JOIN = "interview_join"


def consume(*, jti: str, purpose: str, expires_at: datetime) -> bool:
    """Record a token as used. True on first use, False on every replay.

    Concurrency is resolved by the unique constraint rather than by a lock: two
    simultaneous requests both attempt the insert, exactly one succeeds, and the
    loser sees IntegrityError. No read-then-write race exists.

    The insert is wrapped in its own savepoint so that an IntegrityError does
    not poison a surrounding transaction — the caller is expected to continue
    and return a refusal, not to abort.
    """
    from apps.common.models import ConsumedToken

    try:
        with transaction.atomic():
            ConsumedToken.objects.create(jti=jti, purpose=purpose, expires_at=expires_at)
    except IntegrityError:
        return False
    return True


def purge_expired() -> int:
    """Delete consumed tokens past their expiry.

    Safe because expiry is enforced independently by the signature's own
    timestamp: an expired token is refused before it ever reaches the ledger,
    so forgetting that it was used costs nothing.
    """
    from apps.common.models import ConsumedToken

    deleted, _ = ConsumedToken.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted
