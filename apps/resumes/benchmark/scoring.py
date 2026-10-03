"""
Scoring one parse against the truth it was written from.

Everything is precision and recall, never a bare hit count, because the two
failures are not interchangeable. A missed skill costs the candidate a chip
they can add back. An invented job costs them their credibility with an
employer, and the platform has no way to know it did it. So the corpus measures
both and the summary reports both, and a field with perfect recall and poor
precision is not called good.

Matching is normalised, not fuzzy. Case, spacing and edge punctuation are
noise; a similarity threshold is a way to score a wrong answer as nearly right,
and there is no nearly-right employer name.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from apps.resumes.parsing.base import ParsedResume
from apps.resumes.parsing.extract import NO_TEXT_WARNING

from .corpus import Case, Role, Study

__all__ = ["CaseResult", "FieldScore", "score_case", "score_corpus"]

_SPACE = re.compile(r"\s+")
_EDGE = " \t.,:;|·—–-()[]{}"


def _norm(value: str) -> str:
    return _SPACE.sub(" ", (value or "").strip().strip(_EDGE)).casefold()


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _link(value: str) -> str:
    trimmed = re.sub(r"^https?://", "", (value or "").strip(), flags=re.IGNORECASE)
    return trimmed.removeprefix("www.").rstrip("/").casefold()


@dataclass
class FieldScore:
    """Counts for one field of one case."""

    expected: int = 0
    found: int = 0
    correct: int = 0
    missed: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)
    wrong_value: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return 1.0 if self.found == 0 else self.correct / self.found

    @property
    def recall(self) -> float:
        return 1.0 if self.expected == 0 else self.correct / self.expected

    @property
    def f1(self) -> float:
        if self.expected == 0 and self.found == 0:
            return 1.0
        if self.correct == 0:
            return 0.0
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall)

    def merge(self, other: FieldScore) -> None:
        self.expected += other.expected
        self.found += other.found
        self.correct += other.correct
        self.missed += other.missed
        self.spurious += other.spurious
        self.wrong_value += other.wrong_value


@dataclass
class CaseResult:
    slug: str
    fmt: str
    fields: dict[str, FieldScore]
    #: Text the corpus said must never be proposed as a skill, and was.
    forbidden: list[str] = field(default_factory=list)
    #: A document that is a picture and was not reported as one, or the reverse.
    picture_error: str = ""
    duration_ms: int = 0

    @property
    def f1(self) -> float:
        scores = [score.f1 for score in self.fields.values()]
        return sum(scores) / len(scores) if scores else 0.0

    @property
    def is_clean(self) -> bool:
        return not self.forbidden and not self.picture_error and self.f1 >= 0.999


#: The fields the summary reports, in the order a person reads a CV.
FIELDS = (
    "name",
    "email",
    "phone",
    "link",
    "headline",
    "summary",
    "skills",
    "experience",
    "experience_dates",
    "education",
    "languages",
    "certifications",
    "projects",
    "awards",
)


def _set_score(expected: list[Any], found: list[Any], key: Callable[[Any], str]) -> FieldScore:
    """Precision and recall over two bags of values, compared by `key`.

    Not `list[str]`: education is scored on `Study` rows against the parser's
    own rows, with `_study_key` reading both. `key` is what makes two of them
    the same thing, and `_show` is what turns one back into a line a person
    can read.
    """
    want = {key(value): _show(value) for value in expected if key(value)}
    got = {key(value): _show(value) for value in found if key(value)}
    correct = want.keys() & got.keys()
    return FieldScore(
        expected=len(want),
        found=len(got),
        correct=len(correct),
        missed=[want[k] for k in want.keys() - got.keys()],
        spurious=[got[k] for k in got.keys() - want.keys()],
    )


def _show(value) -> str:
    """A miss or a fabrication, written the way a person would read it."""
    if isinstance(value, str):
        return value
    qualification = getattr(value, "qualification", None)
    if qualification is not None:
        institution = getattr(value, "institution", "")
        return " — ".join(part for part in (qualification, institution) if part)
    return _label(value)


def _role_key(role: Role | object) -> frozenset[str]:
    """A role identified by the pair it names, not by which slot each fell in.

    Half the CVs in the world write "Engineer, Acme" and half write
    "Acme — Engineer". Reading the second one's employer as the job title is a
    presentation problem the candidate fixes in one click; failing to find the
    role at all is a different and much worse failure, and conflating the two
    hides it.
    """
    title = _norm(getattr(role, "title", ""))
    org = _norm(getattr(role, "org", ""))
    return frozenset(part for part in (title, org) if part)


def _study_key(study: Study | object) -> str:
    qualification = _norm(getattr(study, "qualification", ""))
    institution = _norm(getattr(study, "institution", ""))
    return f"{qualification}|{institution}" if institution else qualification


def _score_experience(truth: tuple[Role, ...], found: tuple) -> tuple[FieldScore, FieldScore]:
    """Roles, and separately the dates on the roles that were found.

    Dates are scored only where the role matched. A date on a role that does
    not exist is already counted once as a fabrication, and counting it twice
    would make the date column a shadow of the role column.
    """
    want = {_role_key(role): role for role in truth}
    got = {_role_key(row): row for row in found if _role_key(row)}
    matched = want.keys() & got.keys()

    roles = FieldScore(
        expected=len(want),
        found=len(got),
        correct=len(matched),
        missed=[_label(want[k]) for k in want.keys() - got.keys()],
        spurious=[_label(got[k]) for k in got.keys() - want.keys()],
    )

    dates = FieldScore()
    for key in matched:
        expected_role, found_role = want[key], got[key]
        dates.expected += 1
        dates.found += 1
        if _dates_agree(expected_role, found_role):
            dates.correct += 1
        else:
            dates.wrong_value.append(
                f"{_label(expected_role)}: want {expected_role.start}–"
                f"{'present' if expected_role.is_current else expected_role.end}, "
                f"got {found_role.start}–"
                f"{'present' if found_role.is_current else found_role.end}"
            )
    return roles, dates


def _dates_agree(expected: Role, found) -> bool:
    if _year(expected.start) != _year(getattr(found, "start", "")):
        return False
    if expected.is_current != bool(getattr(found, "is_current", False)):
        return False
    if expected.is_current:
        return True
    return _year(expected.end) == _year(getattr(found, "end", ""))


_YEAR = re.compile(r"(19|20)\d{2}")


def _year(value: str) -> str:
    match = _YEAR.search(value or "")
    return match.group(0) if match else ""


def _label(row) -> str:
    title = getattr(row, "title", "") or ""
    org = getattr(row, "org", "") or ""
    return f"{title} @ {org}".strip(" @")


def _starts_with(found: str, expected: str) -> bool:
    """Whether an extracted item is the one the truth names.

    Not equality. A CV writes "CFA Level I — passed February 2026" and the
    truth calls it "CFA Level I": the item was read correctly and simply
    carries more of the line than the label does. Prefix matching credits that
    and still refuses an item that is about something else.
    """
    return _norm(found).startswith(_norm(expected)) or _norm(expected).startswith(_norm(found))


def _item_score(expected: tuple[str, ...], found: list[str]) -> FieldScore:
    """Precision and recall over a list section — certifications, awards…

    Scored on the structured field now that there is one. It used to be scored
    on the raw section text, which measured only whether the reader had *seen*
    the section: it could see a candidate's three certifications, keep them as
    a blob of text, and hand the builder nothing. That is exactly what it was
    doing, and the benchmark called it full marks.
    """
    matched_expected = [
        value for value in expected if any(_starts_with(row, value) for row in found)
    ]
    matched_found = [row for row in found if any(_starts_with(row, value) for value in expected)]
    return FieldScore(
        expected=len(expected),
        found=len(found),
        correct=len(matched_expected),
        missed=[value for value in expected if value not in matched_expected],
        spurious=[row for row in found if row not in matched_found],
    )


def score_case(case: Case, fmt: str, parsed: ParsedResume, duration_ms: int = 0) -> CaseResult:
    truth = case.truth
    fields: dict[str, FieldScore] = {}

    if truth.is_picture:
        # There is exactly one right answer and it is not a field score.
        said_picture = NO_TEXT_WARNING in parsed.warnings
        return CaseResult(
            slug=case.slug,
            fmt=fmt,
            fields={name: FieldScore() for name in FIELDS},
            picture_error="" if said_picture else "not reported as a picture",
            duration_ms=duration_ms,
        )

    if NO_TEXT_WARNING in parsed.warnings:
        return CaseResult(
            slug=case.slug,
            fmt=fmt,
            fields={name: FieldScore(expected=1) for name in FIELDS},
            picture_error="a readable document was reported as a picture",
            duration_ms=duration_ms,
        )

    fields["name"] = _set_score(
        [truth.name] if truth.name else [], [parsed.name] if parsed.name else [], _norm
    )
    fields["email"] = _set_score(list(truth.emails), list(parsed.emails), _norm)
    fields["phone"] = _set_score(list(truth.phones), list(parsed.phones), _digits)
    fields["link"] = _set_score(list(truth.links), list(parsed.links), _link)
    fields["headline"] = _set_score(
        [truth.headline] if truth.headline else [],
        [parsed.headline] if parsed.headline else [],
        _norm,
    )
    fields["summary"] = _set_score(
        ["summary"] if truth.has_summary else [],
        ["summary"] if parsed.summary.strip() else [],
        _norm,
    )
    fields["skills"] = _set_score(list(truth.skills), list(parsed.skill_terms), _norm)
    fields["experience"], fields["experience_dates"] = _score_experience(
        truth.experience, parsed.experience
    )
    fields["education"] = _set_score(list(truth.education), list(parsed.education), _study_key)
    fields["languages"] = _set_score(list(truth.languages), list(parsed.languages), _norm)

    # The sections a CV built out of anything but employment depends on. Each
    # has a structured field now, so each is scored on what the builder would
    # actually receive rather than on whether the reader saw the heading.
    fields["certifications"] = _item_score(truth.certifications, list(parsed.certifications))
    fields["awards"] = _item_score(truth.awards, list(parsed.awards))
    fields["projects"] = _item_score(truth.projects, [row.name for row in parsed.projects])

    forbidden = [
        term
        for term in truth.never_skills
        if any(_norm(term) == _norm(found) for found in parsed.skill_terms)
    ]

    return CaseResult(
        slug=case.slug, fmt=fmt, fields=fields, forbidden=forbidden, duration_ms=duration_ms
    )


@dataclass
class CorpusSummary:
    results: list[CaseResult]
    totals: dict[str, FieldScore]

    @property
    def macro_f1(self) -> float:
        return sum(result.f1 for result in self.results) / len(self.results)

    @property
    def micro_f1(self) -> float:
        scores = [score.f1 for score in self.totals.values()]
        return sum(scores) / len(scores) if scores else 0.0

    @property
    def fabrications(self) -> int:
        """Everything the reader reported that is not on the document."""
        return sum(len(score.spurious) for score in self.totals.values()) + sum(
            len(result.forbidden) for result in self.results
        )

    @property
    def clean_cases(self) -> int:
        return sum(1 for result in self.results if result.is_clean)


def score_corpus(results: list[CaseResult]) -> CorpusSummary:
    totals = {name: FieldScore() for name in FIELDS}
    for result in results:
        for name, score in result.fields.items():
            totals[name].merge(score)
    return CorpusSummary(results=results, totals=totals)
