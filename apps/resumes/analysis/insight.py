"""
The measurable facts a report states (FR-4.5, FR-4.7).

Facts, not opinions: total experience, where the gaps are, whether titles
progress, how much of the CV is readable prose, and which claimed skills the
document actually evidences. Every one is arithmetic, so every one is the same
next week — which is what makes score history (FR-4.8) mean anything.

The one judgement call in here is `LEVELS`, and it is stated as a table rather
than hidden in a chain of ifs so that changing it is a visible act.
"""

from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache
from typing import Any

from apps.common import periods

from . import text as reader

#: Months of experience, and what the market calls that. Bands rather than a
#: formula: nobody is "3.4 years senior".
LEVELS: tuple[tuple[int, str], ...] = (
    (0, "entry"),
    (24, "junior"),
    (60, "mid"),
    (108, "senior"),
    (180, "lead"),
)

#: A gap shorter than this is a job change, not a gap worth explaining.
GAP_MONTHS = 4

#: Words that mark a title as more senior than the one before it.
SENIORITY = ("intern", "junior", "associate", "", "senior", "lead", "principal", "head", "director")


def experience_summary(content: dict[str, Any], *, today: dt.date | None = None) -> dict[str, Any]:
    """Total, level, gaps and progression (FR-4.7).

    Overlapping roles are merged rather than added: someone who freelanced while
    employed has not worked eleven years in five, and a total that says so is a
    total a recruiter will not trust.
    """
    today = today or dt.date.today()
    roles = reader.entries_in(content, "experience")

    periods = []
    unreadable = 0
    for role in roles:
        period = reader.parse_period(role.dates, today=today)
        if period is None:
            unreadable += 1
        else:
            periods.append(period)

    merged = _merge(periods)
    months = sum(period.months for period in merged)

    return {
        "roles": len(roles),
        "dated_roles": len(periods),
        "undated_roles": unreadable,
        "total_months": months,
        "total_years": round(months / 12, 1),
        "level": _level(months),
        "current": any(period.is_current for period in periods),
        "gaps": _gaps(merged, today=today),
        "progression": _progression([role.title for role in roles]),
    }


def education_summary(content: dict[str, Any]) -> dict[str, Any]:
    entries = reader.entries_in(content, "education")
    return {
        "count": len(entries),
        "institutions": [entry.org for entry in entries if entry.org][:5],
        "highest": entries[0].title if entries else "",
        "has_dates": any(entry.dates for entry in entries),
    }


def readability(content: dict[str, Any]) -> dict[str, Any]:
    bullets = reader.all_bullets(content)
    lengths = [len(bullet.split()) for bullet in bullets]
    measured = [bullet for bullet in bullets if reader.has_measure(bullet)]
    filler = [bullet for bullet in bullets if reader.opens_with_filler(bullet)]
    summary = reader.summary_text(content)

    return {
        "words": reader.word_count(content),
        "bullets": len(bullets),
        "average_bullet_words": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        "longest_bullet_words": max(lengths, default=0),
        "measured_bullets": len(measured),
        "measured_share": round(len(measured) / len(bullets), 2) if bullets else 0.0,
        "filler_bullets": len(filler),
        "has_summary": bool(summary),
        "summary_words": len(summary.split()),
    }


def skill_evidence(content: dict[str, Any]) -> dict[str, Any]:
    """Which listed skills the document actually backs up (FR-4.5).

    A skill named in a bullet is evidenced; a skill that appears only in the
    skills list is a claim. The distinction is the whole value of the section —
    an employer reading "Kubernetes" wants to know where it was used, and a
    candidate who can see which of their skills are unevidenced knows exactly
    which bullet to write next.
    """
    listed = reader.list_items(content, "skills")
    bullets = reader.all_bullets(content)
    # Case is kept: `_unlisted` reads capitalisation as the signal that a token
    # is a proper noun rather than an ordinary word.
    prose = reader.summary_text(content) + " " + " ".join(bullets)

    supported: list[dict[str, str]] = []
    listed_only: list[str] = []
    for skill in listed:
        needle = skill.lower().strip()
        if not needle:
            continue
        pattern = _mention(needle)
        evidence = next((bullet for bullet in bullets if pattern.search(bullet)), "")
        if evidence:
            supported.append({"term": skill, "evidence": evidence[:200]})
        else:
            listed_only.append(skill)

    return {
        "listed": len(listed),
        "supported": supported,
        "listed_only": listed_only,
        # Terms found in the writing but missing from the skills list. The
        # cheapest real suggestion a report can make: the candidate already did
        # the work, they just did not put it in the list a search reads.
        "unlisted_mentions": _unlisted(prose, listed),
        "coverage": round(len(supported) / len(listed), 2) if listed else 0.0,
    }


# --- internals ------------------------------------------------------------


def _merge(found: list[reader.Period]) -> list[reader.Period]:
    """Collapse overlapping ranges so time is counted once.

    Delegated to `apps.common.periods` (P13): matching totals the same thing
    over profile rows, and one answer to "how long did this person work" is the
    only defensible number of answers.
    """
    return periods.merge(found)


def _gaps(merged: list[reader.Period], *, today: dt.date) -> list[dict[str, Any]]:
    """Report the gaps in the shape the analysis JSON has always used."""
    return [
        {"from": gap.start.isoformat(), "to": gap.end.isoformat(), "months": gap.months}
        for gap in periods.gaps(merged, today=today, minimum_months=GAP_MONTHS)
    ]


def _level(months: int) -> str:
    level = LEVELS[0][1]
    for threshold, name in LEVELS:
        if months >= threshold:
            level = name
    return level


def _progression(titles: list[str]) -> str:
    """Whether the titles read as moving forward.

    Entries are newest-first on a CV, so a rising career reads as descending
    seniority down the page. Three states only — anything finer would be a
    guess dressed as a measurement.
    """
    ranks = [rank for rank in (_rank(title) for title in titles if title) if rank is not None]
    if len(ranks) < 2:
        return "unknown"
    if ranks[0] > ranks[-1]:
        return "rising"
    if ranks[0] < ranks[-1]:
        return "falling"
    return "flat"


def _rank(title: str) -> int | None:
    lowered = title.lower()
    for rank, word in enumerate(SENIORITY):
        if word and word in lowered:
            return rank
    return len(SENIORITY) // 2 if lowered else None


@lru_cache(maxsize=512)
def _mention(needle: str) -> re.Pattern[str]:
    """Match a skill as a word, not as a substring.

    `"r" in "grew the customer base"` is true, and that is how a CV listing R
    and C was told both were evidenced — with "Grew the customer base by 30%"
    printed underneath as the proof. A skill has to sit on a word boundary,
    which for terms like C++, .NET and CI/CD means "not flanked by a letter or
    a digit" rather than `\\b` (whose behaviour next to punctuation is the
    opposite of what is wanted at each end).
    """
    escaped = re.escape(needle)
    return re.compile(rf"(?<![0-9A-Za-z]){escaped}(?![0-9A-Za-z])", re.IGNORECASE)


def _unlisted(prose: str, listed: list[str]) -> list[str]:
    """Capitalised multi-letter tokens in the writing that the skills list omits.

    Deliberately crude, and only ever a prompt for the candidate to look: this
    is not the taxonomy resolver, and it does not pretend to be. The resolver
    runs over the AI extraction in the service, where a real skill row is at
    stake.
    """
    # Both forms, because the tokeniser below strips punctuation: without
    # the stripped form, a CV listing ".NET" was told it had forgotten to
    # list "NET".
    known = {item.lower() for item in listed}
    known |= {item.lower().strip(".,;:()[]\"'") for item in listed}
    found: list[str] = []
    seen: set[str] = set()
    for sentence in _sentences(prose):
        # The first word of a sentence is capitalised by grammar, so it carries
        # no information about whether it is a proper noun. Skipping it is the
        # difference between suggesting "Kubernetes" and suggesting "Reduced".
        for token in sentence.split()[1:]:
            word = token.strip(".,;:()[]\"'").strip()
            key = word.lower()
            if len(word) < 3 or key in known or key in seen or not word[0].isupper():
                continue
            if any(character.isdigit() for character in word):
                continue
            seen.add(key)
            # Kept as written: a candidate pasting this into their skills list
            # wants "Kubernetes", not "kubernetes".
            found.append(word)
            if len(found) >= 8:
                return found
    return found


def _sentences(prose: str) -> list[str]:
    """Split prose where a capital letter stops being evidence of a name."""
    return [part.strip() for part in re.split(r"(?<=[.!?;:])\s+|\n+", prose) if part.strip()]
