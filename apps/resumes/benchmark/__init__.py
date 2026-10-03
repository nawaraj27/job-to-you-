"""
A measured answer to "how good is extraction?".

The corpus is generated, not collected: real CVs are personal data and cannot
live in a repository, and a benchmark nobody can re-run is a number somebody
once saw. Each case here is a document written by hand in `corpus.py` together
with the ground truth it was written from, rendered to PDF or DOCX by the same
code every time, and read back through the production reader.

The score is therefore end-to-end — bytes in, structure out — and covers the
part of the pipeline a candidate actually feels: what the platform believes is
on their CV.
"""

from .corpus import CASES, Case, Truth, render
from .scoring import CaseResult, FieldScore, score_case, score_corpus

__all__ = [
    "CASES",
    "Case",
    "CaseResult",
    "FieldScore",
    "Truth",
    "render",
    "score_case",
    "score_corpus",
]
