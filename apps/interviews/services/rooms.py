"""
Building a join link for one person (SRS §11.3).

One function both portals call, so the rules live in one place: the room must
be open, the link expires when the join window closes, the interviewer is the
moderator and the candidate is not, and the identity handed to the provider is
a pseudonym rather than a database id or an email the provider has no need for.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

from django.conf import settings

from apps.common.errors import InvalidState

from .. import policies
from ..providers import RoomProviderError, room_provider

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

    from ..models import InterviewSession


def pseudonym(user: User) -> str:
    """A stable id for this person that means nothing outside this product."""
    digest = hmac.new(
        settings.SECRET_KEY.encode(), f"interview-user:{user.pk}".encode(), hashlib.sha256
    )
    return f"careerai-{digest.hexdigest()[:20]}"


def join_url(*, session: InterviewSession, user: User, is_host: bool) -> str:
    """A signed, expiring URL into this interview's room, for this person.

    Raises `InvalidState` with a sentence to show when the room is closed or the
    provider cannot make a link.
    """
    policies.require_joinable(session)
    window = policies.join_window(session)
    try:
        return room_provider().join_url_for(
            room_ref=session.room_ref,
            display_name=user.full_name or ("Interviewer" if is_host else "Candidate"),
            is_host=is_host,
            user_id=pseudonym(user),
            email="",
            expires_at=window[1] if window else None,
        )
    except RoomProviderError as exc:
        raise InvalidState(
            "The video room could not be opened just now. Try again in a minute; if it keeps "
            "happening, contact the company to arrange another way to talk."
        ) from exc
