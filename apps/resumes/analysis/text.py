"""
Reading a version's content as text, and the small measurements the rest of the
package builds on.

Kept separate because every module here needs the same few things — the bullets,
the dates, the words — and three copies of "walk the sections" would drift into
three slightly different answers to "how long is this CV".
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from apps.common import periods

#: Words that make a bullet a job description rather than an achievement. Short
#: and boring on purpose: a long list of banned words produces a checker that
#: fails good writing, and the point is to catch the obvious ones.
FILLER_OPENERS = frozenset(
    {
        "responsible",
        "worked",
        "helped",
        "assisted",
        "involved",
        "participated",
        "tasked",
        "duties",
    }
)

#: What a measurable outcome looks like: a percentage, an amount of money, or a
#: quantity — either two digits or more, or a single digit with a unit after it.
#:
#: Deliberately generous. A false positive costs a candidate nothing; a false
#: negative tells someone to rewrite a bullet that was already fine, which is
#: the failure that makes people stop trusting the report. An earlier version
#: matched only a fixed list of units and so missed "40,000 transactions",
#: which is exactly the bullet this check exists to reward.
MEASURE = re.compile(
    r"""(
        \d+\s?%                              # a percentage
      | (?:NPR|Rs\.?|USD|INR)\s?[\d,]+       # money, named
      | [$£€]\s?[\d,]+                       # money, symbol
      | \b\d[\d,]*\.?\d*\s*                  # a number, with a unit attached
        (?:k|m|bn|x|ms|s|hrs?|
           users?|customers?|clients?|people|members?|teams?|
           hours?|days?|weeks?|months?|years?|seconds?|minutes?|
           requests?|rows?|records?|tickets?|deals?|sales?|percent)\b
      | \b(?!(?:19|20)\d{2}\b)\d{2,}(?:[,.]\d+)*\b   # any number of 2+ digits,
                                                     # except a bare year
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: A date range as people actually write it on a CV.
_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    "january|february|march|april|june|july|august|september|october|november|december"
)
DATE_RANGE = re.compile(
    rf"(?P<from>(?:(?P<fm>{_MONTHS})[a-z]*\.?\s+)?(?P<fy>(?:19|20)\d{{2}}))"
    r"\s*(?:-|–|—|to|until)\s*"
    rf"(?P<to>present|current|now|(?:(?P<tm>{_MONTHS})[a-z]*\.?\s+)?(?P<ty>(?:19|20)\d{{2}}))",
    re.IGNORECASE,
)

_MONTH_NUMBER = {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}
_MONTH_NUMBER.update(
    {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
)


@dataclass
class Entry:
    """One experience-shaped row, flattened for measurement."""

    id: str
    section_key: str
    title: str
    org: str
    dates: str
    bullets: list[str] = field(default_factory=list)


#: A date range, in whole months. Defined in `apps.common.periods` because
#: matching needs the same arithmetic over profile rows rather than CV text
#: (P13), and two implementations of "how long did this person work" would
#: eventually disagree about the answer for the same person.
Period = periods.Period


def entries(content: dict[str, Any]) -> list[Entry]:
    """Every entry in every visible section, in render order."""
    found: list[Entry] = []
    for section in sorted(content.get("sections", []), key=lambda s: s.get("order", 0)):
        if not section.get("visible", True):
            continue
        for entry in section.get("entries", []) or []:
            found.append(
                Entry(
                    id=str(entry.get("id", "")),
                    section_key=str(section.get("key", "")),
                    title=str(entry.get("title", "")).strip(),
                    org=str(entry.get("org", "")).strip(),
                    dates=str(entry.get("dates", "")).strip(),
                    bullets=[
                        str(b).strip() for b in entry.get("bullets", []) or [] if str(b).strip()
                    ],
                )
            )
    return found


def entries_in(content: dict[str, Any], key: str) -> list[Entry]:
    return [entry for entry in entries(content) if entry.section_key == key]


def list_items(content: dict[str, Any], key: str) -> list[str]:
    """The items of a list-kind section, e.g. skills."""
    for section in content.get("sections", []):
        if section.get("key") == key:
            return [
                str(item).strip() for item in section.get("items", []) or [] if str(item).strip()
            ]
    return []


def section_text(content: dict[str, Any], key: str) -> str:
    for section in content.get("sections", []):
        if section.get("key") == key:
            return str(section.get("text", "") or "").strip()
    return ""


def summary_text(content: dict[str, Any]) -> str:
    """The summary, from either place it can live.

    `blank_document` puts a summary section in `sections`, and the content shape
    also carries a top-level `summary`. Both are legal; a checker that read only
    one would call half the CVs in the system summary-less.
    """
    return section_text(content, "summary") or str(content.get("summary", "") or "").strip()


def all_bullets(content: dict[str, Any]) -> list[str]:
    return [bullet for entry in entries(content) for bullet in entry.bullets]


def word_count(content: dict[str, Any]) -> int:
    return len(all_text(content).split())


def has_measure(text: str) -> bool:
    return bool(MEASURE.search(text))


def opens_with_filler(text: str) -> bool:
    first = re.sub(r"[^a-z]", "", text.strip().split(" ")[0].lower()) if text.strip() else ""
    return first in FILLER_OPENERS


def parse_period(text: str, *, today: dt.date) -> Period | None:
    """Read a date range off an entry, or return None.

    None is a real answer and the callers treat it as one: a CV with unreadable
    dates gets a finding saying so, not a silently wrong total.
    """
    match = DATE_RANGE.search(text or "")
    if match is None:
        return None

    start = _as_date(match.group("fy"), match.group("fm"), default_month=1)
    if start is None:
        return None

    tail = (match.group("to") or "").lower()
    if tail in {"present", "current", "now"}:
        return Period(start=start, end=today, is_current=True)

    end = _as_date(match.group("ty"), match.group("tm"), default_month=12)
    if end is None or end < start:
        return None
    return Period(start=start, end=end)


def _as_date(year: str | None, month: str | None, *, default_month: int) -> dt.date | None:
    if not year:
        return None
    number = _MONTH_NUMBER.get((month or "").lower(), default_month)
    return dt.date(int(year), min(max(number, 1), 12), 1)


def all_text(content: dict[str, Any]) -> str:
    """Every word the CV says, in one string.

    Public because the advert comparison needs the same answer: a skill is on
    a CV if it appears anywhere on it, and two different ideas of "anywhere"
    would make the coverage figure disagree with the findings beside it.
    """
    parts = [summary_text(content)]
    contact = content.get("contact", {}) or {}
    parts.extend(str(value) for value in contact.values())
    for section in content.get("sections", []):
        parts.append(str(section.get("title", "")))
        parts.append(str(section.get("text", "") or ""))
        parts.extend(str(item) for item in section.get("items", []) or [])
        for entry in section.get("entries", []) or []:
            parts.extend(
                [
                    str(entry.get("title", "")),
                    str(entry.get("org", "")),
                    str(entry.get("dates", "")),
                ]
            )
            parts.extend(str(bullet) for bullet in entry.get("bullets", []) or [])
    return " ".join(part for part in parts if part)
