"""
Model rows shaped for the screens that already exist.

The candidate screens were designed and built before the models were, against
session-backed dictionaries. Their markup is the authoritative design
(DEVELOPER.md §7), so this module turns real rows into the shapes those
templates read, in one place, rather than letting every view invent its own.

It holds no rules. Anything that decides something — what is missing, what may
be saved, who may see it — is in the domain services, and this module only
arranges the answers.
"""

from __future__ import annotations

from typing import Any

from apps.accounts.models import UserPreferences
from apps.accounts.services import preferences as preference_service
from apps.candidates import completeness as completeness_rules
from apps.candidates.models import CandidateProfile, Seeking, Visibility, WorkType
from apps.candidates.services import profile as profile_service

#: The visibility choices, with the sentence that says what each one costs.
#: Written out rather than derived from the model's labels: a choice list needs
#: consequences, and `get_visibility_display()` gives four words.
VISIBILITY_OPTIONS = (
    {
        "key": Visibility.EVERYONE,
        "label": "Anyone with the link",
        "text": "Your profile is a public page. Best if you're actively looking.",
    },
    {
        "key": Visibility.EMPLOYERS,
        "label": "Employers you've applied to",
        # Decision D-06: narrowing revokes public, pool and invitation access;
        # only the candidate's own applications keep a company in.
        "text": (
            "Only companies you've applied to can open it. Companies that found you "
            "while it was public, saved you to a talent pool or invited you lose access."
        ),
    },
    {
        "key": Visibility.NOBODY,
        "label": "Nobody",
        "text": (
            "No company can find or open your profile, and talent pools drop you. "
            "Companies you applied to keep your application and what you sent with it. "
            "Matching still runs."
        ),
    },
)

THEME_OPTIONS = (
    {"key": UserPreferences.Theme.SYSTEM, "label": "Match my system", "icon": "monitor"},
    {"key": UserPreferences.Theme.LIGHT, "label": "Light", "icon": "sun"},
    {"key": UserPreferences.Theme.DARK, "label": "Dark", "icon": "moon"},
)

ACCESSIBILITY = (
    {
        "key": "reduce_motion",
        "label": "Reduce motion",
        "text": (
            "Removes transitions and animation. Your system setting is already "
            "respected; this forces it on regardless."
        ),
    },
    {
        "key": "larger_text",
        "label": "Larger text",
        "text": "Raises the base size by 12.5%. Everything scales with it, so nothing overlaps.",
    },
    {
        "key": "underline_links",
        "label": "Underline links in body text",
        "text": "Links stop depending on colour alone to be recognisable.",
    },
    {
        "key": "high_contrast",
        "label": "Higher contrast",
        "text": (
            "Darkens borders and muted text. Separate from Windows High Contrast, "
            "which is detected on its own."
        ),
    },
)

SEEKING_LABELS = dict(Seeking.choices)
WORK_TYPE_LABELS = dict(WorkType.choices)


def profile_payload(profile: CandidateProfile) -> dict[str, Any]:
    """The profile as the profile screen reads it."""
    score, missing = completeness_rules.evaluate(profile)
    return {
        "public_id": str(profile.public_id),
        "row_version": profile.row_version,
        "name": profile.user.full_name,
        "email": profile.user.email,
        "headline": profile.headline,
        "summary": profile.summary,
        "phone": profile.phone,
        "location": str(profile.location) if profile.location_id else "",
        "location_id": profile.location_id,
        "seeking": profile.seeking,
        "seeking_label": SEEKING_LABELS.get(profile.seeking, ""),
        "notice_period_days": profile.notice_period_days,
        "notice": _notice_sentence(profile.notice_period_days),
        "salary_min": profile.salary_min,
        "salary_max": profile.salary_max,
        "currency": profile.currency,
        "period": profile.period,
        "salary_expectation": _salary_sentence(profile),
        "open_to": list(profile.open_to or []),
        "open_to_labels": [WORK_TYPE_LABELS.get(value, value) for value in profile.open_to or []],
        "completeness": score,
        "missing": missing,
        "visibility": profile.visibility,
        "visibility_text": _visibility_option(profile.visibility)["text"],
        "skills": [
            {
                "id": record.skill_id,
                "name": record.skill.name,
                "years": _years_sentence(record.years),
                "level": record.get_level_display() if record.level else "",
                "source": record.source,
            }
            for record in profile.skills.all()
        ],
        "experience": [
            {
                "id": row.pk,
                "title": row.title,
                "org": row.org_name,
                "dates": _date_range(row.start_date, row.end_date, row.is_current),
                "summary": row.summary,
            }
            for row in profile.experiences.all()
        ],
        "education": [
            {
                "id": row.pk,
                "title": row.qualification or "Studied",
                "org": row.institution,
                "dates": _date_range(row.start_date, row.end_date, row.is_current),
            }
            for row in profile.education.all()
        ],
        "certifications": [
            {"id": row.pk, "name": row.name, "issuer": row.issuer}
            for row in profile.certifications.all()
        ],
        "languages": [
            {"id": row.pk, "name": row.language.name, "level": row.get_proficiency_display()}
            for row in profile.languages.all()
        ],
        "links": [
            {"id": row.pk, "label": row.label or row.get_kind_display(), "value": row.url}
            for row in profile.links.all()
        ],
    }


def preferences_payload(preferences: UserPreferences, profile: CandidateProfile | None) -> dict:
    """The `prefs` dictionary the settings templates already read.

    The nesting mirrors the screens, not the tables: privacy spans the profile
    and the preferences row, and a template that had to know which column lives
    where would break every time one moved.
    """
    return {
        "account": {
            "email": preferences.user.email,
            "phone": profile.phone if profile else "",
            "language": preferences.locale,
            "timezone": preferences.timezone,
        },
        "appearance": {
            "theme": preferences.theme,
            "reduce_motion": preferences.reduce_motion,
            "larger_text": preferences.larger_text,
            "underline_links": preferences.underline_links,
            "high_contrast": preferences.high_contrast,
        },
        "privacy": {
            "profile_visibility": profile.visibility if profile else Visibility.EMPLOYERS,
            "allow_contact": profile.allow_contact if profile else True,
            "show_salary": profile.show_salary if profile else False,
            "search_indexing": profile.search_indexing if profile else False,
            "share_analytics": preferences.share_analytics,
        },
    }


def accessibility_rows(preferences: UserPreferences) -> list[dict]:
    return [{**item, "on": getattr(preferences, item["key"])} for item in ACCESSIBILITY]


def visibility_summary(profile: CandidateProfile | None) -> dict:
    if profile is None:
        return dict(VISIBILITY_OPTIONS[1])
    return _visibility_option(profile.visibility)


def next_action(profile: CandidateProfile) -> dict:
    """The one thing this account should do next.

    A dashboard offering eight equally weighted actions has declined to help.
    This picks one from the real gaps, states why, and puts everything else
    below it.
    """
    gaps = profile_service.gaps(profile)

    if not profile.skills.exists():
        return {
            "eyebrow": "Start here",
            "title": "Add the skills you want to be matched on",
            "text": (
                "Matching reads your skills, not your job titles. Three is enough to "
                "start seeing roles; it takes about a minute."
            ),
            "label": "Add skills",
            "icon": "sparkles",
            "url_name": "dashboard:profile",
        }

    if gaps:
        return {
            "eyebrow": "Waiting on you",
            "title": f"Your profile is {profile.completeness}% complete",
            "text": f"Still missing: {', '.join(gaps[:3])}.",
            "label": "Finish your profile",
            "icon": "user",
            "url_name": "dashboard:profile",
        }

    return {
        "eyebrow": "You're set up",
        "title": "Your profile is complete",
        "text": "Matching has everything it needs. Keep it current as things change.",
        "label": "Review your profile",
        "icon": "check-circle",
        "url_name": "dashboard:profile",
    }


#: The step `next_action` sends a new account to first. Skills, because
#: matching reads them and nothing else on the profile changes what a candidate
#: sees; the rest of the path is worth doing and is not worth doing first.
FIRST_STEP = "skills"


def onboarding_steps(profile: CandidateProfile) -> list[dict]:
    """The path a new account walks, with each step's real state.

    Derived from the profile rather than hard-coded, so a step a user has
    already done is never still labelled "current" — which is what the sample
    version did on every visit after the first.

    The step marked "current" is the one `next_action` names in the headline
    above it. They disagreed: a brand-new account was told "Add the skills you
    want to be matched on" in the largest type on the page, with a "Start here"
    badge on "Say who you are" directly underneath. Two first steps is no first
    step.
    """
    done = {
        "basics": bool(profile.headline),
        "skills": profile.skills.count() >= completeness_rules.MIN_SKILLS,
        "history": profile.experiences.exists(),
        "seeking": bool(profile.seeking),
    }
    steps = [
        ("basics", "Say who you are", "A title and where you're based. Thirty seconds."),
        (
            "skills",
            f"Add {completeness_rules.MIN_SKILLS} skills",
            "The only thing job matching actually reads.",
        ),
        ("history", "Add your work history", "Where you've worked, and what you did there."),
        ("seeking", "Say what you're looking for", "Availability, work types, and a salary range."),
    ]

    ordered = sorted(steps, key=lambda step: step[0] != FIRST_STEP)
    first_todo = next((key for key, _title, _text in ordered if not done[key]), None)
    return [
        {
            "title": title,
            "text": text,
            "url_name": "dashboard:profile",
            "state": "done" if done[key] else ("current" if key == first_todo else "todo"),
        }
        for key, title, text in steps
    ]


# --- sentences ------------------------------------------------------------


def _visibility_option(value: str) -> dict:
    return next(
        (dict(option) for option in VISIBILITY_OPTIONS if option["key"] == value),
        dict(VISIBILITY_OPTIONS[1]),
    )


def _notice_sentence(days: int | None) -> str:
    if days is None:
        return ""
    if days == 0:
        return "Available immediately"
    if days % 30 == 0:
        months = days // 30
        return f"{months} month{'s' if months > 1 else ''}"
    return f"{days} days"


def _salary_sentence(profile: CandidateProfile) -> str:
    if profile.salary_min is None and profile.salary_max is None:
        return ""

    period = (profile.period or "").replace("_", " ")
    currency = profile.currency or ""
    low = f"{profile.salary_min:,.0f}" if profile.salary_min is not None else ""
    high = f"{profile.salary_max:,.0f}" if profile.salary_max is not None else ""

    amount = f"{low} – {high}" if low and high else (low or high)
    return " ".join(part for part in [currency, amount, f"per {period}" if period else ""] if part)


def _years_sentence(years) -> str:
    if years is None:
        return ""
    whole = int(years)
    if years == whole:
        return f"{whole} year{'s' if whole != 1 else ''}"
    return f"{years} years"


def _date_range(start, end, is_current: bool) -> str:
    if start is None and end is None:
        return ""
    left = start.strftime("%b %Y") if start else ""
    right = "present" if is_current else (end.strftime("%b %Y") if end else "")
    if left and right:
        return f"{left} — {right}"
    return left or right


def document_classes(preferences: UserPreferences) -> str:
    return preference_service.document_classes(preferences)
