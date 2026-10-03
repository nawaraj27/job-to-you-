"""
Presentation shapes for job screens.

Same split as `context.py` does for the profile: the templates render flat
records, and the mapping from a `Job` row to those records lives here rather
than in a view or, worse, in the template. The reason is the one the rest of
this portal already follows — a template that formats a salary is a template
that has to be edited when the currency rules change (ADR-A39).

Nothing here queries. Callers pass rows that a selector already fetched with
the right `select_related`, so shaping a list of twenty jobs costs no queries.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from django.urls import reverse
from django.utils import timezone

from apps.companies.models import VerificationState
from apps.core.features import is_enabled
from apps.jobs.models import EmploymentType, ExperienceLevel, Job, RemoteScope, Workplace
from apps.matching import scoring as matching_scoring

#: A deadline is only worth printing when it is close, and only urgent when it
#: is very close. "Closes in 30 days" on every listing trains people to ignore
#: the line on the day it says three.
CLOSING_VISIBLE_DAYS = 14
CLOSING_URGENT_DAYS = 3

_PERIOD_LABELS = {
    "year": "a year",
    "month": "a month",
    "week": "a week",
    "day": "a day",
    "hour": "an hour",
}

_VERIFICATION_PILLS = {
    VerificationState.VERIFIED: {
        "label": "Verified employer",
        "tone": "positive",
        "icon": "verified",
    },
    VerificationState.PENDING: {"label": "Verification in review", "tone": "caution"},
    VerificationState.MORE_INFO: {"label": "Verification in review", "tone": "caution"},
    VerificationState.REJECTED: {"label": "Not verified", "tone": "neutral"},
    VerificationState.NOT_SUBMITTED: {"label": "Not verified", "tone": "neutral"},
}


#: What each band is called on screen (FR-8.2). One mapping, so the label a
#: candidate sees on a card is the label they see on the posting.
#: An icon per component, for `components/match_reason.html`. Kept beside the
#: labels so a new component cannot ship without somebody choosing one.
_COMPONENT_ICONS = {
    "skills": "check",
    "experience": "briefcase",
    "domain": "building",
    "location": "pin",
    "salary": "cash",
    "recency": "clock",
}


def job_row(job: Job) -> dict[str, Any]:
    """One job as a list row."""
    return {
        "slug": job.slug,
        "title": job.title,
        "url": job.get_absolute_url(),
        "company": company_card(job.company),
        "salary_display": salary_display(job),
        "location": location_display(job),
        "workplace": _label(Workplace, job.workplace),
        "posted_display": posted_display(job),
        "closing": closing(job),
        "skills": [link.skill.name for link in job.skills.all()],
        # Filled by `with_match` where a score exists. Still `None` everywhere
        # else, and deliberately: a key present with an empty value renders as
        # "0%", which reads as a measurement rather than an absence.
        "match": None,
    }


def match_block(match: Any, *, skill_names: dict[int, str] | None = None) -> dict[str, Any]:
    """One score, shaped for a card or a page (FR-8.1, FR-8.2).

    The breakdown travels with the score, always. A number on its own is the
    thing ADR-A6 exists to avoid: a candidate who cannot see why they scored 48
    has been given a verdict rather than an explanation.
    """
    names = skill_names or {}
    matched = [names[skill_id] for skill_id in match.matched_skill_ids if skill_id in names]
    partial = [names[skill_id] for skill_id in match.partial_skill_ids if skill_id in names]
    missing = [names[skill_id] for skill_id in match.missing_skill_ids if skill_id in names]

    return {
        "score": match.score,
        "band": match.band,
        # `label` rather than `band_label`: `components/job_row.html` and
        # `components/match_reason.html` have read `match.label` since the
        # design system was built, and P13's job is to fill those components
        # with real numbers, not to rename their contract.
        "label": matching_scoring.label_for(match.band),
        "provisional": match.provisional,
        "components": [
            {
                "key": component.key,
                "label": component.label,
                "points": round(component.points),
                "weight": component.weight,
                "percent": component.percent,
                "note": component.note,
            }
            for component in match.components
        ],
        "matched": matched,
        "partial": partial,
        "missing": missing,
        "required_count": len(matched) + len(partial) + len(missing),
        # The component notes, which are the explanation (ADR-A6). `advice` and
        # a written summary are filled by the AI explanation where one exists;
        # absent it, these notes are what the screen says, and they are enough.
        "notes": [
            {
                "title": component.label,
                "text": component.note,
                "icon": _COMPONENT_ICONS.get(component.key, "info"),
                # The one place a note can be acted on. The scoring notes are
                # written once and read by two audiences — the candidate here
                # and the recruiter on the applicant screen — so they say what
                # is true rather than what to do about it. The instruction
                # belongs on the side of the screen that can follow it.
                "fix": _FIXES.get(component.note, ""),
            }
            for component in match.components
        ],
        "advice": "",
    }


#: Component notes a candidate can do something about, and what that is. Keyed
#: on the note itself so the wording and its remedy stay together: a note that
#: gets rewritten loses its hint rather than keeping a stale one.
_FIXES: dict[str, str] = {
    "No salary expectation on the profile, so pay is not counted either way.": (
        "Add what you are looking for to your profile and pay starts counting."
    ),
    "The profile does not say where they are.": (
        "Add your location to your profile. It is the only factor missing."
    ),
    "Not enough industry history to compare, so it is not counted either way.": (
        "Employers on your profile are what this compares; adding your roles fills it in."
    ),
}


def with_match(
    job: Job, match: Any, *, skill_names: dict[int, str] | None = None
) -> dict[str, Any]:
    """A job row carrying its score."""
    return {**job_row(job), "match": match_block(match, skill_names=skill_names)}


def job_detail(job: Job) -> dict[str, Any]:
    """One job as a full page."""
    return {
        **job_row(job),
        "summary": summary(job),
        "description": job.description,
        "responsibilities": list(job.responsibilities or []),
        "requirements": list(job.requirements or []),
        "benefits": list(job.benefits or []),
        "experience_level": _label(ExperienceLevel, job.experience_level),
        "employment_type": _label(EmploymentType, job.employment_type),
        "remote_scope": _label(RemoteScope, job.remote_scope),
        "category": job.category.name if job.category_id else "Not categorised",
        "views": job.views_count,
        "apply": apply_route(job),
    }


def company_card(company) -> dict[str, Any]:
    return {
        "name": company.name,
        "slug": company.slug,
        "initials": _initials(company.name),
        "logo": getattr(company, "logo_url", None) or None,
        "url": reverse("jobs:company", args=[company.slug]),
        "industry": company.industry.name if company.industry_id else "",
        "tagline": company.tagline,
        "founded": company.founded or "—",
        "size": company.size_band or "Not stated",
        "location": company.location.name if company.location_id else "Not stated",
        "website": company.website.replace("https://", "").replace("http://", "").rstrip("/"),
        "verified": company.verification_state,
        "verification": _VERIFICATION_PILLS.get(
            company.verification_state, _VERIFICATION_PILLS[VerificationState.NOT_SUBMITTED]
        ),
        "about": list(company.about or []),
    }


def saved_row(saved) -> dict[str, Any]:
    """A saved job, plus why the reader can no longer act on it.

    A shortlist that quietly drops closed roles loses the record of what
    someone was working through; one that shows them as open is worse.
    """
    is_open = saved.job.is_open
    row = job_row(saved.job)
    if not is_open:
        # Its detail page is a 404 for anything no longer listed, so the row
        # names the role without sending somebody to a dead page.
        row["url"] = ""
    return {**row, "note": saved.note, "saved_at": saved.created_at, "is_open": is_open}


# --- formatting -----------------------------------------------------------


def salary_display(job: Job) -> str:
    """What the employer typed, in their currency (ADR-A39).

    Never the annualised base figure: that exists so two currencies can be
    compared, and showing a converted number as if the employer had offered it
    misstates the offer.
    """
    return format_salary(
        visible=job.salary_visible,
        minimum=job.salary_min,
        maximum=job.salary_max,
        currency=job.currency,
        period=job.period,
    )


def snapshot_salary(snapshot: dict | None) -> str:
    """The same sentence, from a frozen job snapshot.

    An application keeps the posting as it read on the day (ADR-A19), and the
    screen that shows it back was printing the raw fields: "NPR
    180000.00–280000.00 per month" against "NPR 180,000–280,000 a month" on
    every other screen in the product. Same numbers, and the one a candidate
    sees after applying looked like a database dump.
    """
    snapshot = snapshot or {}
    return format_salary(
        visible=bool(snapshot.get("salary_visible")),
        minimum=snapshot.get("salary_min"),
        maximum=snapshot.get("salary_max"),
        currency=snapshot.get("currency"),
        period=snapshot.get("period"),
    )


def format_salary(*, visible: bool, minimum, maximum, currency: str, period: str) -> str:
    """One pay range, written the way this product writes them."""
    if not visible or (minimum is None and maximum is None):
        return "Not disclosed"

    minimum = _decimal(minimum)
    maximum = _decimal(maximum)
    if minimum is None and maximum is None:
        return "Not disclosed"

    label = _PERIOD_LABELS.get(period, period or "")
    if minimum is not None and maximum is not None:
        amount = f"{_money(minimum)}–{_money(maximum)}"
    elif minimum is not None:
        amount = f"from {_money(minimum)}"
    else:
        amount = f"up to {_money(maximum)}"

    return f"{currency or 'NPR'} {amount}" + (f" {label}" if label else "")


def _decimal(value):
    """A number out of a snapshot, which stores whatever JSON held."""
    if value is None or isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def location_display(job: Job) -> str:
    if job.workplace == Workplace.REMOTE:
        return _label(RemoteScope, job.remote_scope)
    if job.location_id:
        return job.location.name
    return "Location not given"


def posted_display(job: Job) -> str:
    if job.published_at is None:
        return "Not published"
    days = (timezone.now() - job.published_at).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks > 1 else ''} ago"
    return job.published_at.strftime("%-d %B %Y" if _supports_dash() else "%d %B %Y")


def closing(job: Job) -> dict[str, Any] | None:
    if job.closes_at is None:
        return None
    remaining = job.closes_at - timezone.now()
    if remaining <= dt.timedelta(0):
        return {"label": "Closed", "urgent": True}
    days = remaining.days
    if days > CLOSING_VISIBLE_DAYS:
        return None
    if days == 0:
        return {"label": "Closes today", "urgent": True}
    return {
        "label": f"Closes in {days} day{'s' if days > 1 else ''}",
        "urgent": days <= CLOSING_URGENT_DAYS,
    }


def summary(job: Job) -> str:
    """The first paragraph, for the lede and the meta description."""
    text = (job.description or "").strip()
    if not text:
        return f"{job.title} at {job.company.name}."
    first = text.split("\n\n")[0].strip()
    return first if len(first) <= 300 else first[:297].rstrip() + "…"


def apply_route(job: Job) -> dict[str, Any]:
    """How a reader applies to this posting.

    An external posting links to the employer's own site. A platform posting
    links to the apply screen — which P8 built, and which this function went on
    reporting as unavailable until P14 noticed. The `applications` flag is still
    the gate, so an operator can close applying without editing a template
    (INV-6).
    """
    if job.apply_mode == "external" and job.external_url:
        return {"mode": "external", "url": job.external_url}

    if is_enabled("applications"):
        return {"mode": "platform", "url": reverse("jobs:apply", kwargs={"slug": job.slug})}
    return {"mode": "unavailable", "url": ""}


# --- internals ------------------------------------------------------------


def _money(value: Decimal) -> str:
    return f"{int(value):,}"


def _label(choices, value: str) -> str:
    return dict(choices.choices).get(value, value)


def _initials(name: str) -> str:
    parts = [part for part in (name or "").split() if part]
    if not parts:
        return "—"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _supports_dash() -> bool:
    """`%-d` is a glibc extension; Windows strftime rejects it."""
    try:
        dt.date(2026, 1, 5).strftime("%-d")
    except ValueError:
        return False
    return True
