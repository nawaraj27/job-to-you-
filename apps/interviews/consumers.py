"""
What the pipeline does when an interview concludes (SRS §13.3, §11.4).

One subscriber, and it is deliberately narrow: a completed interview records an
event on the application's timeline and nothing else. It does **not** move the
stage.

That restraint is the decision. An interview outcome is a strong signal and it
is not a decision — moving somebody to "rejected" because a model scored them
2.4 would be exactly the automated adjudication ADR-A9 refuses, and moving them
forward automatically would be the same mistake wearing a friendlier face. The
recommendation appears on the application, and a person acts on it.
"""

from __future__ import annotations

import logging

from apps.notifications.consumers import subscribe
from apps.notifications.models import OutboxEvent

logger = logging.getLogger(__name__)


@subscribe("interview.completed")
def record_on_timeline(event: OutboxEvent) -> None:
    """Put the interview on the application's timeline, visible to both sides."""
    from apps.applications.services import pipeline

    from .models import InterviewSession

    public_id = (event.payload or {}).get("session_public_id")
    if not public_id:
        return

    session = (
        InterviewSession.objects.select_related("application")
        .filter(public_id=str(public_id))
        .first()
    )
    if session is None or session.application is None:
        # Practice has no application and belongs on nobody's timeline.
        return

    pipeline.record_event(
        application=session.application,
        actor=None,
        actor_kind="system",
        type="interview.completed",
        # Visible to the candidate: they were there, and hiding the fact that
        # the interview happened would be a strange kind of discretion. What is
        # *not* shared by default is the outcome (FR-10.11).
        visible_to_candidate=True,
        payload={"kind": session.kind, "session": str(session.public_id)},
    )
