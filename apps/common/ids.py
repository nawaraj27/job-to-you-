"""
Parsing ids that arrive in forms and query strings.

`Model.objects.filter(pk="abc")` raises `ValueError` for an integer key, so every
view that passed a raw form value straight into a filter answered a hand-edited
URL or a stale form with a 500 (talent pools, applicant notes and pools,
interview slots and turns). `None` filters to nothing, which each of those views
already turns into its own 404 or message.
"""

from __future__ import annotations

from typing import Any


def as_pk(value: Any) -> int | None:
    """A positive integer primary key, or None for anything else."""
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if 0 < number < 2**63 else None


def as_uuid(value: Any) -> str | None:
    """A UUID in canonical text form, or None for anything else.

    The same failure as `as_pk`, for public ids: a UUID column handed "abc" or
    "" raises `ValidationError` inside the ORM, and the CV builder, apply,
    practice interviews, bulk applicant moves and webhook settings all answered
    an empty or hand-edited form with a 500.
    """
    import uuid

    if isinstance(value, uuid.UUID):
        return str(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError):
        return None
