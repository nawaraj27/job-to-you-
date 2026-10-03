"""
The match score (FR-8.1, FR-8.2, SRS §10.2, ADR-A6).

Six weighted components over two feature vectors. Pure: no database, no clock
of its own, no model call. The same two vectors produce the same score forever,
which is what makes `Application.match_snapshot` (FR-8.5) worth freezing and
what makes a golden-file test able to catch a scoring change.

**The breakdown is the explanation.** There is no second, prettier account of
why a job scored 73 — the interface renders these components and these notes.
An LLM explanation (FR-8.6) is written *from* this breakdown and cannot
contradict it, because it is never given the inputs, only the result.

Why arithmetic rather than a model, restated for this side of the product:
matching is O(candidates × jobs), so inference per pair is not affordable at
any volume; a score that moves between two runs makes "you were 12th" a
statement about the weather; and a candidate who asks why they scored 48 is
owed a sentence that survives being challenged.

`ALGORITHM_VERSION` is stored on every snapshot and is part of every cache key.
Changing any weight, threshold or component means bumping it, so a score from
the old scorer is never silently compared with one from the new.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .extract import SkillCredit, required_months
from .features import check_features

#: Bump on any change to weights, thresholds or components below.
ALGORITHM_VERSION = 2

#: SRS §10.2. They sum to 100 and a test says so, because a scorer whose weights
#: sum to 97 produces a number nobody can reason about.
WEIGHTS: dict[str, int] = {
    "skills": 50,
    "experience": 20,
    "domain": 10,
    "location": 8,
    "salary": 7,
    "recency": 5,
}

#: FR-8.2. Bands, not a formula: the label is what a candidate reads first, and
#: it has to mean the same thing on every screen that shows it.
BANDS: tuple[tuple[int, str], ...] = (
    (85, "strong"),
    (70, "good"),
    (50, "partial"),
    (0, "stretch"),
)

#: What each band is called on screen (FR-8.2). In the domain rather than in a
#: portal because both portals show it: a candidate reading "Good match" and an
#: employer reading the same application must read the same words, and the
#: employer portal importing a label out of the candidate portal would be one
#: portal depending on another's presentation.
BAND_LABELS: dict[str, str] = {
    "strong": "Strong match",
    "good": "Good match",
    "partial": "Partial match",
    "stretch": "A stretch",
}

#: A required skill is worth this much more than a preferred one. Employers mark
#: nearly everything required, so the multiplier is deliberately mild: treating
#: "preferred" as free would make the two indistinguishable in practice.
REQUIRED_MULTIPLIER = 2.0

#: Below this credit a skill counts as missing rather than partial. A candidate
#: told they "partially match" a skill on the strength of a third-order
#: implication would rightly call that noise.
PARTIAL_FLOOR = 0.35


@dataclass
class Component:
    key: str
    label: str
    weight: int
    points: float
    note: str

    @property
    def percent(self) -> int:
        return round((self.points / self.weight) * 100) if self.weight else 0


@dataclass
class Match:
    """One score, and everything the interface needs to explain it."""

    score: int
    band: str
    algorithm_version: int
    components: list[Component] = field(default_factory=list)
    matched_skill_ids: list[int] = field(default_factory=list)
    partial_skill_ids: list[int] = field(default_factory=list)
    missing_skill_ids: list[int] = field(default_factory=list)
    #: Set when the result came from the cold-start path, so the interface can
    #: say so instead of presenting a guess as a measurement (FR-8.7).
    provisional: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The shape stored in `Application.match_snapshot` and the cache."""
        return {
            "score": self.score,
            "band": self.band,
            "algorithm_version": self.algorithm_version,
            "components": [
                asdict(component) | {"percent": component.percent} for component in self.components
            ],
            "matched_skill_ids": self.matched_skill_ids,
            "partial_skill_ids": self.partial_skill_ids,
            "missing_skill_ids": self.missing_skill_ids,
            "provisional": self.provisional,
        }


def band_for(score: int, *, missing_required: tuple[int, int] = (0, 0)) -> str:
    """FR-8.2. The band a score falls in — capped by missing requirements.

    `missing_required` is (missing, required). A match can reach 85 on
    experience, location and salary while the posting's one required skill
    is absent, and it used to be labelled "Strong match" (MAT-01): the label
    is what a candidate reads first, and it claimed a fit the explanation
    beneath it contradicted. So:

      * any required skill missing — at best "Good match";
      * half or more of the required skills missing — at best "Partial match".

    The number is left alone. It is a weighted sum and says so; the band is
    the claim, and a claim has to be true.
    """
    band = BANDS[-1][1]
    for threshold, name in BANDS:
        if score >= threshold:
            band = name
            break

    missing, required = missing_required
    if missing and required:
        cap = "partial" if missing * 2 >= required else "good"
        order = [name for _threshold, name in BANDS]
        if order.index(band) < order.index(cap):
            band = cap
    return band


def _missing_required(job: dict[str, Any], missing: list[int]) -> tuple[int, int]:
    required = set(job.get("job_required_skill_ids") or [])
    return (len(required.intersection(missing)), len(required))


def score(
    candidate: dict[str, Any],
    job: dict[str, Any],
    *,
    credit: SkillCredit,
    provisional: bool = False,
) -> Match:
    """Score one candidate against one job.

    `credit` carries the implication graph, loaded once per candidate rather
    than per job — the difference between one query and four hundred when a feed
    is built.
    """
    check_features(candidate)
    check_features(job)

    skills, matched, partial, missing = _skills(candidate, job, credit=credit)
    components = [
        skills,
        _experience(candidate, job),
        _domain(candidate, job),
        _location(candidate, job),
        _salary(candidate, job),
        _recency(candidate, job),
    ]

    total = round(sum(component.points for component in components))
    total = max(0, min(total, 100))
    return Match(
        score=total,
        band=band_for(total, missing_required=_missing_required(job, missing)),
        algorithm_version=ALGORITHM_VERSION,
        components=components,
        matched_skill_ids=matched,
        partial_skill_ids=partial,
        missing_skill_ids=missing,
        provisional=provisional,
    )


# --- components -----------------------------------------------------------


def _skills(
    candidate: dict[str, Any], job: dict[str, Any], *, credit: SkillCredit
) -> tuple[Component, list[int], list[int], list[int]]:
    """Weighted overlap, with partial credit from the implication graph.

    The half of the score that carries the rest, and the only component whose
    output the interface lists item by item: "you have 6 of the 8 skills, and
    these 2 are missing" is the sentence a candidate can act on.

    A job with no skills listed scores neutral rather than zero. The employer
    did not tell us what they want; that is not the candidate's failing, and
    zeroing half the score for it would bury every such posting.
    """
    weight = WEIGHTS["skills"]
    wanted = list(job.get("job_skill_ids") or [])
    if not wanted:
        return (
            Component(
                key="skills",
                label="Skills",
                weight=weight,
                points=weight * 0.6,
                note="This posting does not list the skills it wants, so this is an estimate.",
            ),
            [],
            [],
            [],
        )

    required = set(job.get("job_required_skill_ids") or [])
    weights = job.get("job_skill_weights") or {}
    min_years = job.get("job_skill_min_years") or {}
    candidate_years = candidate.get("skill_years") or {}

    matched: list[int] = []
    partial: list[int] = []
    missing: list[int] = []

    earned = possible = 0.0
    for skill_id in wanted:
        importance = float(weights.get(skill_id, 1.0))
        if skill_id in required:
            importance *= REQUIRED_MULTIPLIER
        possible += importance

        value = credit.credit_for(skill_id)
        if value >= 1.0:
            matched.append(skill_id)
            value = _year_adjusted(value, candidate_years.get(skill_id), min_years.get(skill_id))
        elif value >= PARTIAL_FLOOR:
            partial.append(skill_id)
        else:
            missing.append(skill_id)
            value = 0.0

        earned += importance * value

    share = earned / possible if possible else 0.0
    points = weight * share

    note = f"{len(matched)} of {len(wanted)} skills matched"
    if partial:
        note += f", {len(partial)} partly"
    note += "." if not missing else f". Missing: {len(missing)}."
    return (
        Component(key="skills", label="Skills", weight=weight, points=points, note=note),
        matched,
        partial,
        missing,
    )


def _experience(candidate: dict[str, Any], job: dict[str, Any]) -> Component:
    """Years against the band the posting states.

    Asymmetric on purpose. Being under the band costs proportionally; being over
    it costs a little, because a posting asking for two years is usually not
    served by someone with fifteen — but it is not a disqualification either,
    and treating it as one is how experienced candidates stop seeing anything.
    """
    weight = WEIGHTS["experience"]
    months = int(candidate.get("experience_months") or 0)
    asked = required_months(str(job.get("job_experience_level") or ""))

    if not asked:
        return Component(
            key="experience",
            label="Experience",
            weight=weight,
            points=weight * 0.8,
            note="This posting states no experience requirement.",
        )

    ratio = months / asked if asked else 1.0
    if ratio >= 1.0:
        # Comfortably over: a mild taper, floored well above zero.
        excess = min((ratio - 1.0) / 3.0, 1.0)
        points = weight * (1.0 - 0.25 * excess)
        note = (
            f"{_years(months)} against the {job.get('job_experience_level')} band"
            f"{', more than asked for' if ratio >= 1.5 else ''}."
        )
    else:
        points = weight * max(ratio, 0.0)
        note = f"{_years(months)} against a band asking for about {_years(asked)}."

    return Component(key="experience", label="Experience", weight=weight, points=points, note=note)


def _domain(candidate: dict[str, Any], job: dict[str, Any]) -> Component:
    """Industry adjacency.

    Neutral when the candidate's work history names no industry, which is the
    common case: an unknown is not a mismatch, and scoring it as one would
    penalise every candidate whose past employers are not on the platform.

    Category adjacency, which SRS §10.2 also names, is not scored: a candidate
    carries no job category, and inferring one from what they have applied to
    would make the feed a function of what the feed already showed them.
    """
    weight = WEIGHTS["domain"]
    industries = set(candidate.get("industry_ids") or [])
    wanted = job.get("job_industry_id")

    if not industries or wanted is None:
        return Component(
            key="domain",
            label="Industry",
            weight=weight,
            points=weight * 0.6,
            note="Not enough industry history to compare, so it is not counted either way.",
        )
    if wanted in industries:
        return Component(
            key="domain",
            label="Industry",
            weight=weight,
            points=float(weight),
            note="Has worked in this industry.",
        )
    return Component(
        key="domain",
        label="Industry",
        weight=weight,
        points=weight * 0.4,
        note="A different industry from the ones on the profile.",
    )


def _location(candidate: dict[str, Any], job: dict[str, Any]) -> Component:
    """Workplace and remote scope compatibility.

    Remote work is the case that matters here: a remote posting is a full match
    for a candidate who accepts remote work wherever either of them is, and the
    city comparison must not quietly override that.
    """
    weight = WEIGHTS["location"]
    workplace = str(job.get("job_workplace") or "")
    scope = str(job.get("job_remote_scope") or "none")
    same_city = candidate.get("location_city_id") is not None and candidate.get(
        "location_city_id"
    ) == job.get("job_location_city_id")

    if workplace == "remote" and scope != "none":
        if candidate.get("willing_remote"):
            return Component(
                key="location",
                label="Location",
                weight=weight,
                points=float(weight),
                note="Remote, which this candidate is open to.",
            )
        return Component(
            key="location",
            label="Location",
            weight=weight,
            points=weight * 0.5,
            note="Remote, which the profile does not list as something wanted.",
        )

    if same_city:
        return Component(
            key="location",
            label="Location",
            weight=weight,
            points=float(weight),
            note="Same city.",
        )
    if candidate.get("location_city_id") is None:
        return Component(
            key="location",
            label="Location",
            weight=weight,
            points=weight * 0.6,
            note="The profile does not say where they are.",
        )
    if workplace == "hybrid":
        return Component(
            key="location",
            label="Location",
            weight=weight,
            points=weight * 0.3,
            note="Hybrid, in a different city. Some days on site would mean moving.",
        )
    return Component(
        key="location",
        label="Location",
        weight=weight,
        points=weight * 0.2,
        note="On-site in a different city.",
    )


def _salary(candidate: dict[str, Any], job: dict[str, Any]) -> Component:
    """Overlap of the two annual-base ranges (§5.3).

    Both sides are already normalised to one currency and one period by the
    taxonomy service, which is the only reason this can be arithmetic: NPR per
    month and USD per year do not compare, and comparing them anyway is a bug
    that looks like a preference.

    Neutral when either side is silent — most postings in this market do not
    publish a range, and treating that as a mismatch would score the majority
    of the corpus down for the employer's choice.
    """
    weight = WEIGHTS["salary"]
    wants_min = candidate.get("salary_min_base")
    offers_min, offers_max = job.get("job_salary_min_base"), job.get("job_salary_max_base")

    # Which side is silent, said plainly. "No pay range to compare" on a posting
    # that prints its range in the header reads as a bug in the platform, and
    # the candidate cannot act on it: the missing half is theirs, and saying so
    # is the difference between a dead line and the next useful thing to do.
    if wants_min is None:
        return Component(
            key="salary",
            label="Salary",
            weight=weight,
            points=weight * 0.6,
            note=("No salary expectation on the profile, so pay is not counted either way."),
        )

    ceiling = offers_max if offers_max is not None else offers_min
    if ceiling is None:
        return Component(
            key="salary",
            label="Salary",
            weight=weight,
            points=weight * 0.6,
            note="This employer has not published a range, so pay is not counted.",
        )

    if ceiling >= wants_min:
        return Component(
            key="salary",
            label="Salary",
            weight=weight,
            points=float(weight),
            note="The range reaches what the candidate is asking for.",
        )

    shortfall = (wants_min - ceiling) / wants_min if wants_min else 1.0
    points = weight * max(1.0 - min(shortfall, 1.0), 0.0)
    return Component(
        key="salary",
        label="Salary",
        weight=weight,
        points=points,
        note="The top of this range is below what the candidate is asking for.",
    )


def _recency(candidate: dict[str, Any], job: dict[str, Any]) -> Component:
    """Posting freshness and candidate activity.

    The smallest component, and it earns its 5% by breaking ties in the
    direction that helps: between two equal matches, the one posted this week
    is the one still open.
    """
    weight = WEIGHTS["recency"]
    posted = int(job.get("job_published_days") or 0)
    active = int(candidate.get("last_active_days") or 0)

    freshness = max(0.0, 1.0 - posted / 45.0)
    liveness = max(0.3, 1.0 - active / 120.0)
    points = weight * (0.7 * freshness + 0.3 * liveness)

    if posted <= 3:
        note = "Posted in the last few days."
    elif posted <= 21:
        note = f"Posted {posted} days ago."
    else:
        note = f"Posted {posted} days ago. Older postings are often already filled."
    return Component(key="recency", label="Freshness", weight=weight, points=points, note=note)


# --- helpers --------------------------------------------------------------


def _year_adjusted(value: float, held: float | None, asked: float | None) -> float:
    """Trim a matched skill's credit when the posting asks for more years.

    Never below half: having the skill and less of the history is still having
    the skill, and zeroing it would rank someone with four of five years behind
    someone with none.
    """
    if not asked or held is None:
        return value
    if held >= asked:
        return value
    return value * max(0.5, held / asked)


def _years(months: int) -> str:
    if months < 12:
        return f"{months} months"
    years = months / 12
    return f"{years:.0f} year" + ("s" if round(years) != 1 else "")


def label_for(band: str) -> str:
    """What a band is called on screen."""
    return BAND_LABELS.get(band, band.title() if band else "Match")
