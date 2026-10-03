"""
The written explanation (FR-8.6, SRS §10.2 step 4).

**Top results only, and written from the breakdown — never from the inputs.**
The model is given the score, the components and the skill lists that the
deterministic scorer already produced. It is not given the profile, the posting,
or anything it could use to form its own opinion, because an explanation that
can disagree with the score is not an explanation of that score.

That constraint is the whole design. It means the prose cannot invent a reason,
cannot flatter, and cannot contradict the numbers on the same screen; the worst
it can do is phrase them badly, which is a quality problem rather than a
correctness one.

Everything goes through `ai.services.runner.run` (P11), so caching by input hash
— which FR-8.6 asks for by name — is the runner's, not a second cache here. Two
candidates with identical breakdowns get one call between them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from apps.ai.models import RunStatus
from apps.ai.services import runner

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile

    from ..scoring import Match

logger = logging.getLogger(__name__)

TASK_KEY = "match.explain"

#: How many results are explained. Ten, per SRS §10.2 — the ones a candidate
#: actually reads. Explaining fifty would multiply the cost by five to write
#: text for rows nobody scrolls to.
EXPLAIN_TOP = 10


def explain(
    match: Match,
    *,
    job_title: str,
    user: CandidateProfile | None = None,
    skill_names: dict[int, str] | None = None,
) -> str:
    """One or two sentences about why this score is what it is.

    Returns `""` when AI is unavailable, and the caller renders the breakdown
    alone — which was always the real explanation. The prose is a convenience on
    top of it, never the thing itself (ADR-A6).
    """
    names = skill_names or {}
    run = runner.run(
        task_key=TASK_KEY,
        inputs={
            "job_title": job_title,
            "score": str(match.score),
            "band": match.band,
            "components": _components_text(match),
            "matched": _names(match.matched_skill_ids, names) or "none listed",
            "partial": _names(match.partial_skill_ids, names) or "none",
            "missing": _names(match.missing_skill_ids, names) or "none",
        },
        user=getattr(user, "user", None),
    )

    if run.status != RunStatus.OK:
        logger.info("match explanation unavailable: %s", run.status)
        return ""
    return str(run.output.get("explanation", "")).strip()


def explain_feed(items: list[Any], *, skill_names: dict[int, str] | None = None) -> dict[int, str]:
    """Explanations for the top of a feed, keyed by job id.

    Bounded by `EXPLAIN_TOP` rather than by the caller, so no screen can ask for
    fifty explanations by passing a bigger list.
    """
    explanations: dict[int, str] = {}
    for item in items[:EXPLAIN_TOP]:
        text = explain(item.match, job_title=item.job.title, skill_names=skill_names)
        if text:
            explanations[item.job.pk] = text
    return explanations


# --- internals ------------------------------------------------------------


def _components_text(match: Match) -> str:
    """The breakdown, as lines the prompt can read.

    Deliberately the same numbers the interface renders. If these two ever
    disagreed, the explanation would be describing a score nobody was shown.
    """
    return "\n".join(
        f"- {component.label}: {component.points:.0f} of {component.weight} — {component.note}"
        for component in match.components
    )


def _names(skill_ids: list[int], names: dict[int, str]) -> str:
    return ", ".join(names[skill_id] for skill_id in skill_ids if skill_id in names)
