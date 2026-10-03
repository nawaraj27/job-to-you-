"""
Whether a CV is ready, in one place (CVR-01).

Manual testing found "Complete" on CVs that were not: the guided builder's
"6 of 6 done" ticked every step because each section had *something* in it,
while the same CV's analysis said an applicant tracking system could not read
it, and the advert it was aimed at asked for skills it never mentioned.
Nothing was wrong with either calculation; there were simply two of them, and
the generous one was the one that looked like a verdict.

This module is the verdict. Every screen that says "ready" or "not ready"
reads it, and it is complete only when every requirement below is met:

  * **sections** — the steps a CV must have, from the guided flow's own
    completion (a step the candidate honestly set aside does not count
    against them);
  * **ats** — the latest analysis *of this exact content* has no failing
    applicant-tracking check. No analysis, or an analysis of an older draft,
    is not a pass: it is an unknown, and an unknown is not "complete";
  * **target** — when the analysis was run against an advert, every skill the
    advert *requires* appears in the CV. Evidence missing for a target job is
    not complete, however good the CV is otherwise. A posting on the platform
    marks each skill required or preferred; a missing preferred skill is
    named but does not fail this, the same way a warning does not fail the
    ATS requirement. A pasted advert does not say, so every skill it names
    counts as required.

Two concepts this module deliberately keeps apart: **readiness** (is this CV
complete for what it is aimed at?) and the **match label** on a job
(`matching`, MAT-01), which scores a profile against a posting. A missing
required skill makes both honest — the label is capped and, when the CV is
aimed at that posting, readiness is Incomplete — but neither reads the other.

A requirement that does not apply (no target advert) is left out rather than
marked met, so the list a candidate reads is the list that is about them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .content import content_hash

if TYPE_CHECKING:  # pragma: no cover
    from .models import ResumeAnalysis, ResumeDocument

MET = "met"
UNMET = "unmet"


@dataclass(frozen=True)
class Requirement:
    key: str
    label: str
    status: str
    detail: str = ""
    #: Unmet because it has not been checked (no analysis, or one of an older
    #: draft) rather than because a check failed. The screen then says "run
    #: the analysis again", not "fix what the checks point at" (MIS-01).
    unchecked: bool = False

    @property
    def met(self) -> bool:
        return self.status == MET


@dataclass(frozen=True)
class Readiness:
    requirements: list[Requirement] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.requirements) and all(item.met for item in self.requirements)

    @property
    def outstanding(self) -> list[Requirement]:
        return [item for item in self.requirements if not item.met]

    @property
    def label(self) -> str:
        return "Complete" if self.complete else "Incomplete"


def assess(
    document: ResumeDocument,
    *,
    analysis: ResumeAnalysis | None | Any = ...,
    guided: dict[str, Any] | None = None,
) -> Readiness:
    """The readiness of this document's current draft.

    `analysis` and `guided` may be passed when the caller already has them —
    the builder screens sit under a hard query ceiling (NFR-2), and reading
    either twice on one page is what breaks it.
    """
    from . import selectors
    from .services import builders

    if analysis is ...:
        analysis = selectors.latest_analysis_for(document)
    if guided is None:
        guided = builders.guided_state(document=document)

    content = document.draft.content
    requirements = [_sections(guided)]
    requirements.append(_ats(analysis, content))
    target = _target(analysis, content)
    if target is not None:
        requirements.append(target)
    return Readiness(requirements=requirements)


def _sections(guided: dict[str, Any]) -> Requirement:
    missing = [
        step["title"]
        for step in guided["steps"]
        if not step["complete"] and not step["skipped"] and not step["optional"]
    ]
    if missing:
        return Requirement(
            "sections",
            "Every section a CV needs",
            UNMET,
            "Still to fill in: " + ", ".join(missing) + ".",
        )
    return Requirement("sections", "Every section a CV needs", MET)


def _is_current(analysis: ResumeAnalysis, content: dict) -> bool:
    version = getattr(analysis, "version", None)
    return version is not None and version.sha256 == content_hash(content)


def _ats(analysis: ResumeAnalysis | None, content: dict) -> Requirement:
    label = "Readable by applicant tracking systems"
    if analysis is None:
        return Requirement(
            "ats", label, UNMET, "Not checked yet. Run an analysis to check it.", unchecked=True
        )
    if not _is_current(analysis, content):
        return Requirement(
            "ats",
            label,
            UNMET,
            "The CV has changed since it was last checked. Run the analysis again.",
            unchecked=True,
        )
    from .models import CheckStatus

    checks = list(analysis.checks.all())
    failing = [check.label for check in checks if check.status == CheckStatus.FAIL]
    if failing:
        return Requirement("ats", label, UNMET, "Failing: " + "; ".join(failing) + ".")
    # Met — nothing fails — but a warning is still said, so "Complete" beside
    # a middling score does not read as "nothing to improve".
    warned = [check.label for check in checks if check.status == CheckStatus.WARN]
    detail = ("No check fails. Worth improving: " + "; ".join(warned) + ".") if warned else ""
    return Requirement("ats", label, MET, detail)


def _target(analysis: ResumeAnalysis | None, content: dict) -> Requirement | None:
    if analysis is None or not _is_current(analysis, content):
        return None
    target = analysis.target or {}
    if not target or target.get("percent") is None:
        return None
    label = "Evidence for the job you are aiming at"
    missing = list(target.get("missing") or [])
    # Reports written before the required/preferred split have no
    # `missing_required`; for them every missing skill counts, which is the
    # stricter reading and the one they were shown at the time.
    required = list(target.get("missing_required", missing) or [])
    preferred = [name for name in missing if name not in required]
    if required:
        detail = "The advert requires, and the CV does not mention: " + ", ".join(required) + "."
        if preferred:
            detail += " It would also like: " + ", ".join(preferred) + "."
        return Requirement("target", label, UNMET, detail)
    if preferred:
        return Requirement(
            "target",
            label,
            MET,
            "Every required skill is there. Worth adding, if you have it: "
            + ", ".join(preferred)
            + ".",
        )
    return Requirement("target", label, MET)
