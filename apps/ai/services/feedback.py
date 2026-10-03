"""
Feedback on AI output (SRS §5.11, ADR-A11).

The evaluation corpus, gathered from the first day rather than assembled later.
A thumbs-down collected at the moment somebody was annoyed carries information
that a labelling exercise six months afterwards does not, and it costs one
click to collect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.common.errors import InvalidState, NotFound
from apps.common.ids import as_uuid

from ..models import AIFeedback, AIRun, Verdict

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User


@transaction.atomic
def record(*, run: AIRun, user: User, verdict: str, comment: str = "") -> AIFeedback:
    """One person's verdict, changeable but not duplicable."""
    if verdict not in Verdict.values:
        raise InvalidState("A verdict is useful or not useful.")

    feedback, _ = AIFeedback.objects.update_or_create(
        run=run,
        user=user,
        defaults={"verdict": verdict, "comment": (comment or "").strip()[:500]},
    )
    return feedback


def run_for(*, user: User, public_id: str) -> AIRun:
    """Scoped to the person the run belongs to.

    An `AIRun` carries the input snapshot, which is the candidate's own CV
    text — so a run somebody else caused is NotFound rather than forbidden.
    """
    if as_uuid(public_id) is None:
        raise NotFound("That result does not exist, or you cannot see it.")
    run = AIRun.objects.filter(public_id=public_id, user=user).first()
    if run is None:
        raise NotFound("That result does not exist, or you cannot see it.")
    return run


def summary(*, task_key: str = "") -> dict[str, int]:
    """How the corpus is looking. The number that says whether the AI is
    useful or noise (SRS §16.3)."""
    queryset = AIFeedback.objects.all()
    if task_key:
        queryset = queryset.filter(run__task_key=task_key)

    good = queryset.filter(verdict=Verdict.GOOD).count()
    bad = queryset.filter(verdict=Verdict.BAD).count()
    return {"good": good, "bad": bad, "total": good + bad}
