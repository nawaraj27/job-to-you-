"""
Turning a profile and a job into feature vectors (SRS §10.2, FR-8.9).

The seam that makes the scorer testable and the fairness rule enforceable.
Everything that touches the database happens here; everything after this is
arithmetic on two dictionaries whose keys are on the allow-list.

Two costs shaped this module:

  * **The implication graph is loaded once, not per job.** Scoring one candidate
    against four hundred postings must not walk `SkillRelation` four hundred
    times. `SkillCredit` is built once from the candidate's own skills and then
    read as a dictionary.
  * **Job features are extracted once per job, not once per pair.** A feed
    scores one candidate against many jobs; a ranking scores many candidates
    against one. Both directions reuse the side that stays constant.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from apps.taxonomy.services import skills as skill_service

from .features import check_features

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

#: How far an implication chain is followed. Two hops: Django implies Python
#: implies programming, and by the third the credit is small enough that the
#: query cost buys nothing.
IMPLICATION_DEPTH = 2

#: Seniority bands, in months. Shared with the resume analyser's vocabulary on
#: purpose — a candidate told they read as "mid" on their CV and matched as
#: "junior" here would be right to call that broken.
SENIORITY_BANDS: tuple[tuple[int, str], ...] = (
    (0, "entry"),
    (24, "junior"),
    (60, "mid"),
    (108, "senior"),
    (168, "lead"),
)

#: What a job's `experience_level` asks for, in months. The mapping is here
#: rather than on the model because it is a matching judgement, not a fact about
#: the posting.
LEVEL_MONTHS: dict[str, int] = {
    "internship": 0,
    "entry": 0,
    "junior": 12,
    "mid": 48,
    "senior": 96,
    "lead": 144,
    "executive": 180,
}


@dataclass
class SkillCredit:
    """What a candidate's skills are worth against any job's skills.

    `direct` maps a skill id to 1.0. `implied` maps a skill id the candidate did
    not claim to the credit their claimed skills earn against it — Django
    satisfying Python at 0.85, and nothing satisfying it at 1.0 except Python.

    Built once per candidate and read many times, which is the only reason
    scoring a feed is one query rather than four hundred.
    """

    direct: dict[int, float] = field(default_factory=dict)
    implied: dict[int, float] = field(default_factory=dict)

    def credit_for(self, skill_id: int) -> float:
        """How well this candidate covers one required skill, 0.0 to 1.0."""
        if skill_id in self.direct:
            return 1.0
        return self.implied.get(skill_id, 0.0)

    @property
    def covered(self) -> set[int]:
        return set(self.direct) | set(self.implied)


def skill_credit_for(profile: CandidateProfile) -> SkillCredit:
    """Load the candidate's skills and everything they partially satisfy."""
    from apps.candidates.models import CandidateSkill

    rows = list(CandidateSkill.objects.filter(profile=profile).select_related("skill")[:200])
    credit = SkillCredit(direct={row.skill_id: 1.0 for row in rows})

    for row in rows:
        for skill_id, weight in skill_service.implications(
            row.skill, max_depth=IMPLICATION_DEPTH
        ).items():
            if skill_id in credit.direct:
                continue
            value = float(weight)
            if value > credit.implied.get(skill_id, 0.0):
                credit.implied[skill_id] = value

    return credit


def candidate_features(
    profile: CandidateProfile, *, today: dt.date | None = None
) -> dict[str, Any]:
    """Everything the scorer may know about a candidate.

    Note what is absent, and that its absence is the point: no name, no photo,
    no age, no institution, no address below city (SRS §10.4). `check_features`
    enforces it rather than trusting this function to stay honest.
    """
    today = today or timezone.localdate()

    skills = list(profile.skills.all())
    months = _experience_months(profile, today=today)
    updated = profile.updated_at.date() if profile.updated_at else today

    vector: dict[str, Any] = {
        "skill_ids": sorted(row.skill_id for row in skills),
        "skill_years": {row.skill_id: float(row.years or 0) for row in skills},
        "skill_levels": {row.skill_id: int(row.level or 0) for row in skills},
        "experience_months": months,
        "seniority": seniority_for(months),
        "industry_ids": _industry_ids(profile),
        "location_city_id": profile.location_id,
        "open_to": list(profile.open_to or []),
        "willing_remote": _willing_remote(profile),
        "salary_min_base": _as_float(profile.salary_min_base),
        "salary_max_base": _as_float(profile.salary_max_base),
        "profile_updated_days": max((today - updated).days, 0),
        "last_active_days": max((today - updated).days, 0),
    }
    check_features(vector)
    return vector


def job_features(job: Job, *, today: dt.date | None = None) -> dict[str, Any]:
    """Everything the scorer may know about a posting."""
    from apps.jobs.models import Requirement

    today = today or timezone.localdate()
    job_skills = list(job.skills.all())
    published = job.published_at.date() if job.published_at else today

    vector: dict[str, Any] = {
        "job_skill_ids": sorted(row.skill_id for row in job_skills),
        "job_required_skill_ids": sorted(
            row.skill_id for row in job_skills if row.requirement == Requirement.REQUIRED
        ),
        "job_skill_weights": {row.skill_id: float(row.weight or 1) for row in job_skills},
        "job_skill_min_years": {
            row.skill_id: float(row.min_years) for row in job_skills if row.min_years is not None
        },
        "job_experience_level": job.experience_level or "",
        "job_category_id": job.category_id,
        "job_industry_id": getattr(job.company, "industry_id", None),
        "job_location_city_id": job.location_id,
        "job_workplace": job.workplace,
        "job_remote_scope": job.remote_scope,
        "job_salary_min_base": _as_float(job.salary_min_base),
        "job_salary_max_base": _as_float(job.salary_max_base),
        "job_published_days": max((today - published).days, 0),
    }
    check_features(vector)
    return vector


def seniority_for(months: int) -> str:
    """The band this much experience reads as."""
    band = SENIORITY_BANDS[0][1]
    for threshold, name in SENIORITY_BANDS:
        if months >= threshold:
            band = name
    return band


def required_months(level: str) -> int:
    """What a posting's stated level asks for, in months."""
    return LEVEL_MONTHS.get((level or "").lower(), 0)


# --- internals ------------------------------------------------------------


def _experience_months(profile: CandidateProfile, *, today: dt.date) -> int:
    """Total distinct months of work, overlaps counted once.

    Delegated to `candidates.services.profile` (P15), which owns the one reading
    of a profile's work history. P13 built its own list of periods over the same
    rows; P15 needed the same number for talent search, and three builders of
    one list is how they start disagreeing about whether a current role counts
    to today.
    """
    from apps.candidates.services.profile import experience_months

    return experience_months(profile)


def _industry_ids(profile: CandidateProfile) -> list[int]:
    """Industries the candidate has actually worked in.

    Read from the companies on their work history, so only roles at employers
    the platform knows contribute. That is a thin signal and the scorer treats
    it as one: an empty list scores neutral, never zero, because "we do not know
    your industry" is not "you are in the wrong industry".
    """
    found = {
        row.company.industry_id
        for row in profile.experiences.all()
        if row.company is not None and row.company.industry_id
    }
    return sorted(found)


def _willing_remote(profile: CandidateProfile) -> bool:
    return any(str(item).lower() == "remote" for item in profile.open_to or [])


def _as_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None
