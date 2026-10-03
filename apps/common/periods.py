"""
Date ranges, and counting time once.

Two domains need this and they arrived a phase apart: resume analysis totals
the experience written on a CV (P12), and matching totals the experience
recorded on a profile (P13). The inputs differ — one parses "Aug 2023 –
Present" out of free text, the other reads two date columns — but the arithmetic
after that is identical, and the interesting part of it is a rule about people
rather than about dates:

**Overlapping roles are merged, never added.** Somebody who freelanced while
employed has not worked eleven years in five. A total that says so is a total a
recruiter stops trusting, and — in matching — one that pushes a candidate into
a seniority band they have not reached.

This module is pure: no models, no ORM, no clock of its own.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Period:
    """A date range, measured in whole months."""

    start: dt.date
    end: dt.date
    is_current: bool = False

    @property
    def months(self) -> int:
        return max((self.end.year - self.start.year) * 12 + self.end.month - self.start.month, 0)


def merge(periods: list[Period]) -> list[Period]:
    """Collapse overlapping ranges so time is counted once.

    Touching ranges merge as well as overlapping ones: leaving one job on the
    31st and starting the next on the 1st is not a career gap, and reporting it
    as two separate periods would produce a spurious gap finding.
    """
    if not periods:
        return []

    ordered = sorted(periods, key=lambda period: period.start)
    merged = [ordered[0]]
    for period in ordered[1:]:
        last = merged[-1]
        if period.start <= last.end:
            if period.end > last.end:
                merged[-1] = Period(
                    start=last.start,
                    end=period.end,
                    is_current=last.is_current or period.is_current,
                )
        else:
            merged.append(period)
    return merged


def total_months(periods: list[Period]) -> int:
    """Months of distinct time covered by these ranges."""
    return sum(period.months for period in merge(periods))


def gaps(periods: list[Period], *, today: dt.date, minimum_months: int = 4) -> list[Period]:
    """Stretches of nothing between the ranges, and after the last one.

    `minimum_months` exists because a six-week gap between jobs is a notice
    period, not a career break, and reporting it as one is how a report loses
    the reader's confidence on its first line.
    """
    merged = merge(periods)
    if not merged:
        return []

    found: list[Period] = []
    for earlier, later in zip(merged, merged[1:], strict=False):
        gap = Period(start=earlier.end, end=later.start)
        if gap.months >= minimum_months:
            found.append(gap)

    last = merged[-1]
    if not last.is_current:
        trailing = Period(start=last.end, end=today)
        if trailing.months >= minimum_months:
            found.append(trailing)

    return found
