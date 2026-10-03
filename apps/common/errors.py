"""
Domain error hierarchy (DEVELOPER.md §3.7).

Services raise these; the portal layer maps them to responses in exactly one
place. Every error carries a message written for the person who will read it,
because a service is the only layer that knows enough to write a good one.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base for every expected failure of a domain operation.

    Expected is the operative word. A DomainError means the system worked and
    the answer is no. Bugs raise something else and reach the error tracker.
    """

    status_code: int = 400
    code: str = "domain_error"
    default_message: str = "That action could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        if code:
            self.code = code
        self.details: dict[str, Any] = details or {}
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotAllowed(DomainError):
    """The actor may not do this. Authorisation, not existence."""

    status_code = 403
    code = "not_allowed"
    default_message = "You do not have permission to do that."


class NotFound(DomainError):
    """A scoped lookup found nothing.

    Raised instead of NotAllowed when revealing existence would itself leak:
    a 403 confirms the object is real (DEVELOPER.md §6.2).
    """

    status_code = 404
    code = "not_found"
    default_message = "That does not exist, or you cannot see it."


class InvalidState(DomainError):
    """A legal object in a state where this action has no meaning."""

    status_code = 409
    code = "invalid_state"
    default_message = "That action is not available right now."


class Conflict(DomainError):
    """A duplicate, or a race another request already won."""

    status_code = 409
    code = "conflict"
    default_message = "That has already been done."


class StaleWrite(Conflict):
    """Optimistic lock mismatch: the row changed since it was loaded."""

    code = "stale_write"
    default_message = (
        "Someone else changed this while you were editing. Reload to see their version."
    )


class QuotaExceeded(DomainError):
    """A limit was reached. The actor may retry later, or never."""

    status_code = 429
    code = "quota_exceeded"
    default_message = "You have reached the limit for this action."


class ProviderError(DomainError):
    """An external dependency failed. Usually retryable, never the user's fault."""

    status_code = 502
    code = "provider_error"
    default_message = "An external service is unavailable. This will be retried."


__all__ = [
    "Conflict",
    "DomainError",
    "InvalidState",
    "NotAllowed",
    "NotFound",
    "ProviderError",
    "QuotaExceeded",
    "StaleWrite",
]
