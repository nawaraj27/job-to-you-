"""
The limit vocabulary (SRS §5.14, P18).

Every commercial limit the platform can enforce is declared here, once. A plan
that names a key not in this table is a typo, and a typo in a JSONB document
would otherwise be indistinguishable from "unlimited" — which is the failure
mode where a paid feature is quietly free for a year.

The declaration carries the refusal wording too. A limit is only as good as the
sentence somebody reads when they hit it, and that sentence belongs beside the
limit rather than in whichever view happened to catch the error.

**What is not here.** Abuse limits (`apps/common/ratelimit.py`) are a different
subject with a different answer: "you are going too fast, wait" versus "this is
not on your plan". A limit that cannot be lifted by paying is not an
entitlement, and putting the two in one table would eventually have a free user
told to upgrade because they typed too quickly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .models import UsagePeriod


@dataclass(frozen=True)
class Limit:
    """One thing a plan can grant, withhold, or meter."""

    key: str
    kind: str  # "count" | "flag" | "set" | "money"
    label: str
    #: The message somebody sees when the plan says no. Written for them, not
    #: for the log.
    refusal: str
    #: Which period a metered limit resets on. Ignored for the other kinds.
    period: str = UsagePeriod.MONTH
    #: What an account with no opinion in its plan gets. Deliberately the
    #: *permissive* value for flags and sets only where the feature is free to
    #: everyone; metered defaults are `None`, meaning unlimited, because a plan
    #: that forgot to mention a limit has not imposed one.
    default: Any = None
    #: Whose limit it is — `candidate`, `company`, or blank for both. The usage
    #: panel showed a candidate "Open postings: unlimited on this plan" and an
    #: employer "CV analyses", because every metered key was listed for everyone.
    audience: str = ""


RESUME_ANALYSES = Limit(
    key="resume_analyses",
    kind="count",
    label="CV analyses",
    refusal="You have used this month's CV analyses. Your reports stay available.",
    audience="candidate",
)

PRO_TEMPLATES = Limit(
    key="pro_templates",
    kind="flag",
    label="Pro templates",
    refusal="That template is part of Pro. The free templates are yours to use as they are.",
    default=False,
)

EXPORT_FORMATS = Limit(
    key="export_formats",
    kind="set",
    label="Download formats",
    refusal="Downloading your CV is part of Pro.",
    default=(),
)

AI_COST_MONTHLY = Limit(
    key="ai_cost_monthly",
    kind="money",
    label="Monthly AI budget",
    refusal="The AI budget on your plan is spent for this month.",
)

ACTIVE_JOBS = Limit(
    key="active_jobs",
    kind="count",
    label="Open postings",
    refusal="Your plan's limit on open postings has been reached.",
    audience="company",
)

# --- AI-backed features (each has its own allowance) ---------------------
#
# One meter per feature, not one shared pool: an hour spent on the assistant
# should not use up somebody's interview practice. Periods follow how each
# thing is used: conversation and rewording happen in bursts over a day;
# practice interviews and cover letters are weekly-to-monthly work. The
# numbers live in the plans (`seeds.PLANS`), never here or in a view.

ASSISTANT_REPLIES = Limit(
    key="assistant_replies",
    kind="count",
    label="Assisted CV replies",
    refusal="You have used today's assisted CV replies.",
    period=UsagePeriod.DAY,
    audience="candidate",
)

CV_IMPROVEMENTS = Limit(
    key="cv_improvements",
    kind="count",
    label="CV improvement drafts",
    refusal="You have used today's CV improvement drafts.",
    period=UsagePeriod.DAY,
    audience="candidate",
)

INTERVIEW_PRACTICE = Limit(
    key="interview_practice",
    kind="count",
    label="Practice interviews",
    refusal="You have used this month's practice interviews.",
    audience="candidate",
)

COVER_LETTERS = Limit(
    key="cover_letters",
    kind="count",
    label="Cover letter drafts",
    refusal="You have used this month's cover letter drafts.",
    audience="candidate",
)

CANDIDATE_SEARCH = Limit(
    key="candidate_search",
    kind="flag",
    label="Talent search",
    refusal="Talent search is not part of your plan.",
    default=False,
)


REGISTRY: dict[str, Limit] = {
    limit.key: limit
    for limit in (
        RESUME_ANALYSES,
        ASSISTANT_REPLIES,
        CV_IMPROVEMENTS,
        INTERVIEW_PRACTICE,
        COVER_LETTERS,
        PRO_TEMPLATES,
        EXPORT_FORMATS,
        AI_COST_MONTHLY,
        ACTIVE_JOBS,
        CANDIDATE_SEARCH,
    )
}

#: Keys that meter usage and therefore write a `UsageCounter` row.
METERED = frozenset(key for key, limit in REGISTRY.items() if limit.kind == "count")


class UnknownLimit(KeyError):
    """A plan named a limit that does not exist."""


def get(key: str) -> Limit:
    try:
        return REGISTRY[key]
    except KeyError:
        raise UnknownLimit(
            f"{key!r} is not a declared limit. Add it to apps/billing/limits.py "
            "rather than inventing it in a plan document."
        ) from None


def validate(document: dict[str, Any]) -> None:
    """Refuse a plan document that names something undeclared.

    Called when a plan is written rather than when it is read: a bad limit
    should stop the person editing it, not the candidate who happens to be the
    first to hit it.
    """
    for key, value in document.items():
        if key == "schema_version":
            continue
        limit = get(key)
        _check_shape(limit, value)


def _check_shape(limit: Limit, value: Any) -> None:
    if value is None:
        return  # Unlimited, for every kind that can be unlimited.
    if limit.kind == "count" and not isinstance(value, int):
        raise ValueError(f"{limit.key} is a count; {value!r} is not a whole number.")
    if limit.kind == "flag" and not isinstance(value, bool):
        raise ValueError(f"{limit.key} is a yes/no; {value!r} is neither.")
    if limit.kind == "set" and not isinstance(value, list | tuple):
        raise ValueError(f"{limit.key} is a list of allowed values; {value!r} is not a list.")
    if limit.kind == "money":
        try:
            Decimal(str(value))
        except Exception:  # noqa: BLE001 - any parse failure is the same answer
            raise ValueError(f"{limit.key} is an amount; {value!r} is not one.") from None
