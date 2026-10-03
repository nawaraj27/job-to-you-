"""
The deterministic half of resume analysis (FR-4.2, FR-4.3, FR-4.5, FR-4.7).

Everything in this package is a pure function of a version's content. No model
call, no network, no clock beyond the one date arithmetic needs. That is what
makes a score reproducible, what makes it golden-file testable, and what lets
the analysis screen still say something true when AI is switched off.

The split follows ADR-A6, applied to a CV rather than to a job match: the
deterministic part produces the score, the checks and the measurable facts; the
model produces prose and judgement findings and cannot move the number.
"""

from __future__ import annotations

from .checks import Check, ats_checks
from .findings import Finding, deterministic_findings
from .insight import education_summary, experience_summary, readability, skill_evidence
from .scoring import ALGORITHM_VERSION, WEIGHTS, score_version

__all__ = [
    "ALGORITHM_VERSION",
    "WEIGHTS",
    "Check",
    "Finding",
    "ats_checks",
    "deterministic_findings",
    "education_summary",
    "experience_summary",
    "readability",
    "score_version",
    "skill_evidence",
]
