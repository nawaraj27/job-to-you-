"""
How complete a profile is, and what is missing.

Derived on every write, never typed. The frontend stand-in this replaces
hard-coded 82% next to a list of gaps that included "add your notice period"
while the notice period was filled in two inches above — a static number
describing data that changes is a number that is eventually wrong, and that one
already was.

Weights reflect **how much a field affects matching**, not how hard it is to
fill in, so the meter moves most when something that matters is added. Skills
are weighted highest because they are the only thing the scorer actually reads.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CandidateProfile

#: Skills below this count leave the profile unrankable in practice: two skills
#: match almost nothing, and the candidate reads the empty result as "no jobs".
MIN_SKILLS = 3

#: Below this, an employer-side search would be ranking a stub against real
#: profiles. The candidate still sees jobs — matching runs regardless.
SEARCHABLE_THRESHOLD = 50


@dataclass(frozen=True)
class Gap:
    key: str
    label: str
    weight: int


#: (key, label, weight)
FIELDS: tuple[Gap, ...] = (
    Gap("headline", "your current title", 2),
    Gap("summary", "a short summary", 1),
    Gap("location", "your location", 1),
    Gap("seeking", "whether you're looking", 2),
    Gap("open_to", "the work types you'd accept", 1),
    Gap("notice_period_days", "your notice period", 1),
    Gap("salary", "your salary expectation", 1),
    Gap("skills", f"at least {MIN_SKILLS} skills", 4),
    Gap("experiences", "your work history", 3),
    Gap("education", "your education", 1),
    Gap("links", "a link to your work", 1),
)

TOTAL_WEIGHT = sum(gap.weight for gap in FIELDS)


def _filled(profile: CandidateProfile, key: str) -> bool:
    if key == "salary":
        return profile.salary_min is not None or profile.salary_max is not None
    if key == "skills":
        return profile.skills.count() >= MIN_SKILLS
    if key in {"experiences", "education", "links"}:
        return getattr(profile, key).exists()
    if key == "location":
        return profile.location_id is not None
    if key == "open_to":
        return bool(profile.open_to)
    if key == "notice_period_days":
        return profile.notice_period_days is not None
    return bool(str(getattr(profile, key) or "").strip())


def evaluate(profile: CandidateProfile) -> tuple[int, list[str]]:
    """Returns (percentage, human-readable gaps, most valuable first)."""
    earned = sum(gap.weight for gap in FIELDS if _filled(profile, gap.key))
    missing = sorted(
        (gap for gap in FIELDS if not _filled(profile, gap.key)),
        key=lambda gap: -gap.weight,
    )
    labels = [gap.label for gap in missing]

    # Skills are the one field where "present" is not "enough". Saying "at
    # least three skills" to someone who has two is a worse prompt than saying
    # how many more are needed.
    count = profile.skills.count()
    if 0 < count < MIN_SKILLS:
        remaining = MIN_SKILLS - count
        labels = [f"{remaining} more skill{'s' if remaining > 1 else ''}"] + [
            label for label in labels if not label.startswith("at least")
        ]

    return round(earned / TOTAL_WEIGHT * 100), labels
