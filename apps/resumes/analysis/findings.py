"""
Deterministic findings (FR-4.4).

The half of the report that does not need a model: things that are true about
the document by inspection. Each one carries a `code` that stays stable across
versions, so "this has been wrong for three versions" is a query rather than a
guess, and a `section_ref` in the suggestion-path vocabulary, so "apply this
fix" (FR-4.9) has somewhere to write.

Strengths are findings too. A report that lists only faults is a report people
stop opening, and the strengths tell a candidate which parts not to rewrite.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from . import checks as ats
from . import insight
from . import text as reader

CRITICAL, IMPROVE, STRENGTH = "critical", "improve", "strength"


@dataclass
class Finding:
    severity: str
    code: str
    title: str
    text: str = ""
    evidence: str = ""
    action: str = ""
    section_ref: str = ""
    from_ai: bool = False


def deterministic_findings(
    content: dict[str, Any],
    *,
    ats_results: list[ats.Check],
    today: dt.date | None = None,
) -> list[Finding]:
    """Everything that can be said about a CV without asking a model."""
    today = today or dt.date.today()
    facts = insight.readability(content)
    evidence = insight.skill_evidence(content)
    experience = insight.experience_summary(content, today=today)

    found: list[Finding] = []
    found += _contact_findings(content, ats_results)
    found += _summary_findings(content, facts)
    found += _bullet_findings(content)
    found += _structure_findings(content)
    found += _skill_findings(content, evidence)
    found += _history_findings(content, experience, today=today)
    found += _strengths(facts, evidence, experience)

    order = {CRITICAL: 0, IMPROVE: 1, STRENGTH: 2}
    return sorted(found, key=lambda finding: order[finding.severity])


def _contact_findings(content: dict[str, Any], results: list[ats.Check]) -> list[Finding]:
    check = next((result for result in results if result.key == "contact"), None)
    if check is None or check.status == ats.PASS:
        return []

    contact = content.get("contact", {}) or {}
    field = "email" if not str(contact.get("email", "")).strip() else "phone"
    return [
        Finding(
            severity=CRITICAL if check.status == ats.FAIL else IMPROVE,
            code="contact_incomplete",
            title="A recruiter cannot reach you",
            text=check.note,
            action="Add the missing contact details at the top of the CV.",
            section_ref=f"contact.{field}",
        )
    ]


def _summary_findings(content: dict[str, Any], facts: dict[str, Any]) -> list[Finding]:
    if not facts["has_summary"]:
        return [
            Finding(
                severity=IMPROVE,
                code="no_summary",
                title="There is no summary",
                text=(
                    "The first thing read is the top of the page. Without a summary a "
                    "recruiter builds their own impression from whatever your most recent "
                    "role happens to say."
                ),
                action="Write two or three sentences saying what you do and what you are after.",
                section_ref="summary",
            )
        ]
    if facts["summary_words"] < 15:
        return [
            Finding(
                severity=IMPROVE,
                code="thin_summary",
                title="The summary is too short to say anything",
                text=f"It runs to {facts['summary_words']} words.",
                evidence=reader.summary_text(content)[:200],
                action="Say what you do, how long you have done it, and what you want next.",
                section_ref="summary",
            )
        ]
    stock = generic_phrases(reader.summary_text(content))
    if len(stock) >= GENERIC_PHRASE_LIMIT:
        # Only on the phrases actually there: two or more of the stock words
        # every reader skips, quoted back so the candidate can see which. A
        # summary that happens to say "motivated" once is not flagged.
        return [
            Finding(
                severity=IMPROVE,
                code="generic_summary",
                title="The summary is written in stock phrases",
                text=(
                    "Phrases like "
                    + ", ".join(f"“{phrase}”" for phrase in stock[:4])
                    + " appear on most CVs, so they tell a reader nothing about you."
                ),
                evidence=reader.summary_text(content)[:200],
                action="Replace them with what you actually do, for whom, and for how long.",
                section_ref="summary",
            )
        ]
    return []


#: Stock phrases a reader skips. Matched as whole phrases, case-insensitively.
GENERIC_PHRASES = (
    "hard-working",
    "hardworking",
    "hard working",
    "team player",
    "passionate",
    "highly motivated",
    "self-motivated",
    "motivated",
    "results-driven",
    "results driven",
    "detail-oriented",
    "detail oriented",
    "self-starter",
    "go-getter",
    "dynamic",
    "proven track record",
    "excellent communication skills",
    "think outside the box",
    "dedicated professional",
)
#: How many distinct stock phrases make a summary generic.
GENERIC_PHRASE_LIMIT = 2


def generic_phrases(text: str) -> list[str]:
    """The stock phrases in `text`, longest first, without overlaps."""
    lowered = text.lower()
    found: list[str] = []
    for phrase in sorted(GENERIC_PHRASES, key=len, reverse=True):
        if any(phrase in longer for longer in found):
            continue
        if re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", lowered):
            found.append(phrase)
    return found


def _structure_findings(content: dict[str, Any]) -> list[Finding]:
    """What is missing from an entry, read straight off the entry.

    Each one names the entry, because "some roles are thin" sends the
    candidate hunting; each one is raised only for what is actually absent on
    the page — a role with no title, a role or project with nothing under it —
    never for a section this product would merely like to see.
    """
    found: list[Finding] = []
    for entry in reader.entries_in(content, "experience"):
        where = entry.title or entry.org or "One role"
        if not entry.title.strip() or not entry.org.strip():
            missing = "job title" if not entry.title.strip() else "employer"
            slot = "title" if missing == "job title" else "org"
            found.append(
                Finding(
                    severity=IMPROVE,
                    code="untitled_role",
                    title=f"“{where}” has no {missing}",
                    text=(
                        f"A role without its {missing} is hard to place, and an applicant "
                        "tracking system may file it under the wrong heading or drop it."
                    ),
                    action=f"Add the {missing} exactly as it appeared on your contract or payslip.",
                    section_ref=f"entry.{entry.id}.{slot}",
                )
            )
        if not entry.bullets:
            found.append(
                Finding(
                    severity=IMPROVE,
                    code="thin_entry",
                    title=f"“{where}” says nothing about what you did",
                    text=(
                        "The role is listed with no description, so a reader learns you had "
                        "the job and nothing about how you did it."
                    ),
                    action=(
                        "Add two or three lines: what you built, ran or changed, "
                        "and what came of it."
                    ),
                    section_ref=f"entry.{entry.id}.bullets",
                )
            )

    for entry in reader.entries_in(content, "projects"):
        if entry.bullets:
            continue
        name = entry.title or entry.org or "untitled"
        found.append(
            Finding(
                severity=IMPROVE,
                code="thin_project",
                title=f"The project “{name}” is only a name",
                text=(
                    "A project is evidence only when it says what you did in it. A title on "
                    "its own asks the reader to guess."
                ),
                action="Say what you built, the tools you used, and what it does or achieved.",
                section_ref=f"entry.{entry.id}.bullets",
            )
        )

    has_education = bool(
        reader.entries_in(content, "education") or reader.list_items(content, "education")
    )
    if not has_education and reader.entries_in(content, "experience"):
        found.append(
            Finding(
                severity=IMPROVE,
                code="no_education",
                title="There is no education section",
                text=(
                    "Most applications are screened on education as well as experience. "
                    "If you have a qualification (school, college, a degree, a diploma "
                    "— it belongs here; if you do not, leave this."
                ),
                action="Add your highest qualification, where you studied, and the years.",
                section_ref="section.education",
            )
        )
    return found


def _bullet_findings(content: dict[str, Any]) -> list[Finding]:
    """One finding per offending entry, not one per bullet.

    A report with fourteen near-identical rows is a report nobody finishes, and
    the fix is applied to an entry's bullets as a block anyway.
    """
    found: list[Finding] = []
    for entry in reader.entries(content):
        if not entry.bullets or entry.section_key != "experience":
            continue

        unmeasured = [bullet for bullet in entry.bullets if not reader.has_measure(bullet)]
        filler = [bullet for bullet in entry.bullets if reader.opens_with_filler(bullet)]
        where = f"{entry.title} at {entry.org}".strip(" at")

        if filler:
            found.append(
                Finding(
                    severity=IMPROVE,
                    code="duty_language",
                    title=f"“{where}” is written as duties",
                    text=(
                        f"{len(filler)} of {len(entry.bullets)} bullets open with a word like "
                        "“responsible for”, which describes the job rather than what you did in it."
                    ),
                    evidence=filler[0][:200],
                    action="Lead with the outcome, then how you got it.",
                    section_ref=f"entry.{entry.id}.bullets",
                )
            )
        elif len(unmeasured) == len(entry.bullets):
            found.append(
                Finding(
                    severity=IMPROVE,
                    code="no_outcomes",
                    title=f"Nothing in “{where}” is measured",
                    text=(
                        "None of these bullets carry a number. A figure is the difference "
                        "between what you were given to do and what happened."
                    ),
                    evidence=entry.bullets[0][:200],
                    action="Add scale, speed or money to at least one bullet.",
                    section_ref=f"entry.{entry.id}.bullets",
                )
            )
    return found


def _skill_findings(content: dict[str, Any], evidence: dict[str, Any]) -> list[Finding]:
    found: list[Finding] = []
    skills_section = next(
        (section for section in content.get("sections", []) if section.get("key") == "skills"),
        None,
    )

    if evidence["listed"] == 0:
        found.append(
            Finding(
                severity=CRITICAL,
                code="no_skills",
                title="There is no skills list",
                text=(
                    "Employer searches read the skills list. A CV without one is invisible "
                    "to the search that would have found it."
                ),
                action="List the tools and languages you actually use.",
                section_ref=f"section.{skills_section['id']}.items" if skills_section else "",
            )
        )
        return found

    if evidence["listed_only"]:
        found.append(
            Finding(
                severity=IMPROVE,
                code="unevidenced_skills",
                title="Some skills are claimed but never shown",
                text=(
                    "These appear in the list and nowhere else: "
                    + ", ".join(evidence["listed_only"][:6])
                    + ". An interviewer will ask about exactly these."
                ),
                action="Name one of them in a bullet where you used it, or drop it.",
                section_ref=f"section.{skills_section['id']}.items" if skills_section else "",
            )
        )
    return found


def _history_findings(
    content: dict[str, Any], experience: dict[str, Any], *, today: dt.date
) -> list[Finding]:
    found: list[Finding] = []

    if experience["undated_roles"]:
        undated = next(
            (
                entry
                for entry in reader.entries_in(content, "experience")
                if reader.parse_period(entry.dates, today=today) is None
            ),
            None,
        )
        found.append(
            Finding(
                severity=IMPROVE,
                code="unreadable_dates",
                title="Some roles have dates nothing can read",
                text=(
                    f"{experience['undated_roles']} of {experience['roles']} roles do not have "
                    "a date range an applicant tracking system can parse, so they may be "
                    "dropped from your history entirely."
                ),
                evidence=(undated.dates if undated else "")[:200],
                action="Use a form like “Mar 2021 – Present”, the same way in every role.",
                section_ref=f"entry.{undated.id}.dates" if undated else "",
            )
        )

    for gap in experience["gaps"][:2]:
        found.append(
            Finding(
                severity=IMPROVE,
                code="employment_gap",
                title=f"A {gap['months']}-month gap is visible",
                text=(
                    f"Nothing on the CV covers {gap['from']} to {gap['to']}. A gap is not a "
                    "problem; an unexplained one invites the reader to invent a reason."
                ),
                action="Add what you were doing: study, contract work, caring, travel.",
            )
        )

    if experience["roles"] == 0:
        found.append(_no_roles_finding(content))
    return found


#: The sections that carry a first CV. A student, a graduate and a career
#: changer put their evidence here, and a CV built out of them is finished, not
#: broken.
EARLY_CAREER_SECTIONS = ("projects", "education", "certifications", "awards", "publications")


def _no_roles_finding(content: dict[str, Any]) -> Finding:
    """No employment — which is two different documents, not one.

    This used to be a single CRITICAL finding reading "There is no work
    history … there is nothing to assess", shown to every student, graduate and
    school leaver on the platform. It was wrong twice over. It is wrong as a
    fact — a CV with a degree, four projects and a skills list has a great deal
    to assess — and it is wrong as advice, because the only way to act on
    "add your roles" when you have never had a job is to invent one. Telling
    somebody to put experience they do not have on their CV is the single thing
    this product must never do.

    So the finding depends on what is actually on the page. A CV with the
    sections an early-career CV is made of gets advice about those sections. A
    CV with nothing on it at all gets told so, which is the case the critical
    severity was written for.
    """
    filled = [
        key
        for key in EARLY_CAREER_SECTIONS
        if reader.entries_in(content, key) or reader.list_items(content, key)
    ]

    if not filled:
        return Finding(
            severity=CRITICAL,
            code="no_experience",
            title="There is nothing on this CV yet",
            text=(
                "There is no work history, no education and no projects, so there is "
                "nothing for a reader to assess."
            ),
            action="Start with whatever you have: a course, a project, volunteering, a job.",
        )

    named = _readable_list(filled)
    return Finding(
        severity=IMPROVE,
        code="no_paid_experience",
        title="Your evidence is your projects and study, not a job",
        text=(
            "There is no employment on this CV, which is normal and expected early on. "
            f"What a reader has instead is your {named} — so that is the part that has to "
            "do the work, and it is judged the same way a role is: what you did, and what "
            "happened as a result."
        ),
        action=(
            "Give each one a line saying what you built or ran and what came of it. "
            "Add paid work only when you have some."
        ),
        section_ref=f"section.{filled[0]}",
    )


def _readable_list(keys: list[str]) -> str:
    words = [key.replace("_", " ") for key in keys]
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def _strengths(
    facts: dict[str, Any], evidence: dict[str, Any], experience: dict[str, Any]
) -> list[Finding]:
    found: list[Finding] = []

    if facts["bullets"] and facts["measured_share"] >= ats.MEASURED_BULLET_TARGET:
        found.append(
            Finding(
                severity=STRENGTH,
                code="measured_writing",
                title="Your bullets carry numbers",
                text=(
                    f"{facts['measured_bullets']} of {facts['bullets']} bullets quantify "
                    "something. That is the part most CVs get wrong."
                ),
            )
        )

    if experience["progression"] == "rising":
        found.append(
            Finding(
                severity=STRENGTH,
                code="rising_titles",
                title="Your titles show progression",
                text="The roles read as increasing responsibility, which is what a reader looks for.",
            )
        )

    if evidence["listed"] and evidence["coverage"] >= 0.6:
        found.append(
            Finding(
                severity=STRENGTH,
                code="evidenced_skills",
                title="Your skills are backed up by your experience",
                text=(
                    f"{len(evidence['supported'])} of {evidence['listed']} listed skills appear "
                    "in a bullet describing where you used them."
                ),
            )
        )
    return found
