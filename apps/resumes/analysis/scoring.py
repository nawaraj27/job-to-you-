"""
The score (FR-4.3), and the reason it is arithmetic (ADR-A6).

Four weighted components, each producing points out of its weight and a note
explaining the points. The breakdown **is** the explanation the report renders:
there is no second, prettier account of the score that could disagree with it.

Why deterministic, when a model could clearly produce a number:

  * It is reproducible. The same version scores the same next month, which is
    the only way score history (FR-4.8) means anything.
  * It is testable. A golden file catches a scoring change; nothing catches a
    model quietly drifting half a point.
  * It is defensible. "Your CV scored 61 because 3 of 14 bullets carry a
    number" is a sentence that survives being challenged. "The model said 61"
    is not.
  * It still works with AI switched off, which is what SRS §9.3.6 requires of
    every AI feature.

`ALGORITHM_VERSION` is stored on every analysis. Changing weights or components
means bumping it, so a score from the old scorer is never silently compared with
a score from the new one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any

from . import checks as ats
from . import insight

#: Bump on any change to weights, components or thresholds below.
ALGORITHM_VERSION = 1

#: What each component is worth. They sum to 100 and the test suite says so,
#: because a scorer whose weights sum to 97 produces a score nobody can reason
#: about and everybody can argue with.
WEIGHTS: dict[str, int] = {
    "machine_readability": 25,
    "evidence_and_impact": 30,
    "skills_coverage": 20,
    "structure_and_length": 25,
}


@dataclass
class Component:
    key: str
    label: str
    weight: int
    points: int
    note: str

    @property
    def share(self) -> float:
        return round(self.points / self.weight, 2) if self.weight else 0.0

    @property
    def percent(self) -> int:
        """The bar width the report draws. Computed here rather than in the
        template, because a template filter that does arithmetic is arithmetic
        nothing tests."""
        return round(self.share * 100)


def score_version(
    content: dict[str, Any],
    *,
    ats_results: list[ats.Check] | None = None,
    target_skills: list[str] | None = None,
    parse_warnings: list[str] | None = None,
    filename: str = "",
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Score one version's content. Pure, and the same answer every time.

    `target_skills` are the skills of the role being aimed at — a job's required
    skills when the analysis names one. Absent, skills coverage is scored on
    whether the CV evidences what it claims, which is the only thing that can
    honestly be measured without knowing the target.
    """
    today = today or dt.date.today()
    results = ats_results or ats.ats_checks(
        content, parse_warnings=parse_warnings, filename=filename, today=today
    )
    facts = insight.readability(content)
    evidence = insight.skill_evidence(content)

    components = [
        _machine_readability(results),
        _evidence_and_impact(content, facts),
        _skills_coverage(evidence, target_skills or []),
        _structure_and_length(content, facts, today=today),
    ]

    total = sum(component.points for component in components)
    return {
        "score": max(0, min(total, 100)),
        "algorithm_version": ALGORITHM_VERSION,
        "components": [
            asdict(component) | {"share": component.share, "percent": component.percent}
            for component in components
        ],
    }


def _machine_readability(results: list[ats.Check]) -> Component:
    """Points for the ATS checks, weighted by how much each one costs in reality.

    Extraction and contact are not "one check each": a CV whose text does not
    extract, or which carries no email, fails in a real system regardless of how
    well everything else is written.
    """
    weight = WEIGHTS["machine_readability"]
    critical = {"extraction", "contact"}
    by_key = {check.key: check for check in results}

    points = weight
    failures: list[str] = []
    for check in results:
        if check.status == ats.PASS:
            continue
        cost = (weight // 3) if check.key in critical else max(weight // 10, 1)
        cost = cost * 2 if check.status == ats.FAIL else cost
        points -= cost
        failures.append(check.label.lower())

    points = max(points, 0)
    if not failures:
        note = "Every machine-readability check passes."
    else:
        note = "Losing points on " + ", ".join(failures[:4]) + "."
        if by_key.get("extraction") and by_key["extraction"].status != ats.PASS:
            note += " Extraction is the expensive one: unread text scores nothing anywhere."
    return Component(
        key="machine_readability",
        label="Machine readability",
        weight=weight,
        points=points,
        note=note,
    )


def _evidence_and_impact(content: dict[str, Any], facts: dict[str, Any]) -> Component:
    """The part that distinguishes a CV from a job description.

    Two thirds of it is the share of bullets carrying a number, one third is not
    opening bullets with duty words. Both are measurable, and both are the thing
    every recruiter says when asked what is wrong with most CVs.
    """
    weight = WEIGHTS["evidence_and_impact"]
    bullets = facts["bullets"]
    if not bullets:
        return Component(
            key="evidence_and_impact",
            label="Evidence and impact",
            weight=weight,
            points=0,
            note="There are no bullet points, so there is nothing showing what you achieved.",
        )

    measured_share = facts["measured_share"]
    filler_share = facts["filler_bullets"] / bullets

    measured_points = round(min(measured_share / ats.MEASURED_BULLET_TARGET, 1.0) * weight * 2 / 3)
    style_points = round((1 - min(filler_share, 1.0)) * weight / 3)
    points = measured_points + style_points

    # The bar, said out loud. "2 of 4 bullets carry a number." beside a full
    # 30 / 30 reads as a scoring mistake to anybody who does the division, and
    # a candidate who cannot see the target cannot aim at it.
    target = int(ats.MEASURED_BULLET_TARGET * 100)
    met = measured_share >= ats.MEASURED_BULLET_TARGET
    note = (
        f"{facts['measured_bullets']} of {bullets} bullets carry a number"
        f"{f', and {facts["filler_bullets"]} open with a duty word' if facts['filler_bullets'] else ''}"
        f"{f' — at or above the {target}% we look for.' if met else f'. Aim for {target}%.'}"
    )

    # Full marks and an open finding about the same thing, on the same page.
    # The score is the whole CV and the finding is one role, and both were
    # true — but a candidate reading "30 / 30" directly above "Nothing in
    # Backend Engineer at Himalaya Ledger is measured" is reading a report
    # that contradicts itself, and stops trusting the half that was right.
    # Saying it here is what reconciles them.
    unmeasured = _entries_with_no_measure(content)
    if met and unmeasured:
        note += (
            f" The bar is the whole CV, and {_name_them(unmeasured)} still"
            f" {'has' if len(unmeasured) == 1 else 'have'} none — see below."
        )
    return Component(
        key="evidence_and_impact",
        label="Evidence and impact",
        weight=weight,
        points=points,
        note=note,
    )


def _entries_with_no_measure(content: dict[str, Any]) -> list[str]:
    """Roles whose every bullet is a duty, on a CV that passes overall.

    Only roles, and only ones that have bullets: a role with nothing under it
    is a different finding, and education is not judged on figures.
    """
    from . import text as reader

    named = []
    for entry in reader.entries_in(content, "experience"):
        if entry.bullets and not any(reader.has_measure(bullet) for bullet in entry.bullets):
            named.append(entry.title or entry.org or "one role")
    return named


#: How many to name before the sentence becomes a list.
MAX_NAMED_ENTRIES = 2


def _name_them(titles: list[str]) -> str:
    if len(titles) == 1:
        return f"“{titles[0]}”"
    if len(titles) <= MAX_NAMED_ENTRIES:
        return " and ".join(f"“{title}”" for title in titles)
    return f"{len(titles)} of your roles"


def _skills_coverage(evidence: dict[str, Any], target_skills: list[str]) -> Component:
    """Whether the CV evidences what it claims, and — if a target is named —
    whether it covers what the target asks for.

    When no target is given this deliberately does **not** guess at market
    demand. Missing skills expressed as demand require matching data (FR-4.6)
    and arrive with P13; inventing them from a model's opinion of the labour
    market would be exactly the kind of confident nonsense this architecture is
    built to avoid.
    """
    weight = WEIGHTS["skills_coverage"]
    listed = evidence["listed"]

    if not listed:
        return Component(
            key="skills_coverage",
            label="Skills coverage",
            weight=weight,
            points=0,
            note="No skills are listed, so a keyword search will not find this CV at all.",
        )

    if target_skills:
        wanted = {skill.lower().strip() for skill in target_skills if skill.strip()}
        have = {item["term"].lower() for item in evidence["supported"]}
        have |= {item.lower() for item in evidence["listed_only"]}
        matched = wanted & have
        share = len(matched) / len(wanted) if wanted else 0.0
        points = round(share * weight)
        missing = sorted(wanted - have)
        # Said as what it measures — the skills *list* — because the readiness
        # check reads the whole CV: a skill shown in a bullet but not listed
        # is present for readiness and "not listed" here, and the report used
        # to call it "missing" in one place and fine in the other.
        note = f"{len(matched)} of {len(wanted)} skills this role asks for are in your skills list"
        note += f". Not listed: {', '.join(sorted(missing)[:5])}." if missing else "."
        return Component(
            key="skills_coverage",
            label="Skills coverage",
            weight=weight,
            points=points,
            note=note,
        )

    coverage = evidence["coverage"]
    points = round(coverage * weight)
    note = f"{len(evidence['supported'])} of {listed} listed skills are backed up by a bullet"
    if evidence["listed_only"]:
        note += f". Claimed but not shown: {', '.join(evidence['listed_only'][:5])}."
    else:
        note += "."
    return Component(
        key="skills_coverage", label="Skills coverage", weight=weight, points=points, note=note
    )


def _structure_and_length(
    content: dict[str, Any], facts: dict[str, Any], *, today: dt.date
) -> Component:
    weight = WEIGHTS["structure_and_length"]
    points = weight
    problems: list[str] = []

    words = facts["words"]
    if words < ats.SHORT_CV:
        points -= weight // 3
        problems.append("it is short enough to look unfinished")
    elif words > ats.LONG_CV:
        points -= weight // 4
        problems.append("it runs past two pages")

    if not facts["has_summary"]:
        points -= weight // 5
        problems.append("there is no summary")
    elif facts["summary_words"] < 15:
        points -= weight // 10
        problems.append("the summary is too short to say anything")

    if facts["longest_bullet_words"] > 40:
        points -= weight // 10
        problems.append("at least one bullet runs past 40 words")

    experience = insight.experience_summary(content, today=today)
    if experience["undated_roles"]:
        points -= weight // 5
        problems.append(f"{experience['undated_roles']} role(s) have no readable dates")
    if experience["gaps"]:
        points -= weight // 10
        problems.append(f"{len(experience['gaps'])} gap(s) of four months or more")

    points = max(points, 0)
    note = (
        "Well structured for its length."
        if not problems
        else "Points lost because " + ", and ".join(problems[:3]) + "."
    )
    return Component(
        key="structure_and_length",
        label="Structure and length",
        weight=weight,
        points=points,
        note=note,
    )
