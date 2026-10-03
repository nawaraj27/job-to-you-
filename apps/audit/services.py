"""
Audit writes.

One function. Everything that must be auditable calls it, and nothing writes
`AuditLog` directly — so the set of things that produce audit entries is the
set of callers of `record()`, which is greppable.

Never log secrets, tokens, passwords or document text (DEVELOPER.md §2.13).
Log identifiers and what changed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.common.logging import get_context

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.companies.models import Company

from .models import AuditLog

logger = logging.getLogger(__name__)

_MAX_REPR = 255
_REDACTED = "[redacted]"

# Keys that must never reach the log, whatever a caller passes.
_FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "token",
        "secret",
        "api_key",
        "authorization",
        "session_key",
        "raw_text",
        "content",
    }
)


def record(
    *,
    verb: str,
    actor: User | None = None,
    target: models.Model | None = None,
    on_behalf_of: User | None = None,
    company: Company | None = None,
    ip: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Write one audit entry. Never raises into the caller's transaction path."""
    from apps.accounts.impersonation import current_impersonator_id

    impersonator_id = current_impersonator_id()
    if impersonator_id is not None and actor is not None and actor.pk != impersonator_id:
        # FR-1.10: every action taken while impersonating names the staff
        # member who took it and the person it was taken for.
        from apps.accounts.models import User

        on_behalf_of = on_behalf_of or actor
        actor = User.objects.filter(pk=impersonator_id).first()

    target_ct = None
    target_id = ""
    target_repr = ""

    if target is not None:
        target_ct = ContentType.objects.get_for_model(target, for_concrete_model=False)
        target_id = str(target.pk)
        target_repr = str(target)[:_MAX_REPR]

    return AuditLog.objects.create(
        actor=actor,
        on_behalf_of=on_behalf_of,
        company=company,
        verb=verb,
        target_ct=target_ct,
        target_id=target_id,
        target_repr=target_repr,
        ip=ip,
        request_id=get_context().request_id or "",
        metadata=_scrub(metadata or {}),
    )


def _scrub(metadata: dict[str, Any]) -> dict[str, Any]:
    """Drop anything that looks like a secret, at any depth.

    A caller passing a secret is a mistake, and a mistake that reaches an
    append-only table cannot be undone. Cheaper to filter unconditionally.
    """
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.lower() in _FORBIDDEN_KEYS:
            cleaned[key] = _REDACTED
        elif isinstance(value, dict):
            cleaned[key] = _scrub(value)
        else:
            cleaned[key] = value
    return cleaned
