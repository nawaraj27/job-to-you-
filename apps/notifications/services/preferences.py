"""
Notification preferences: six groups, two channels the user controls.

The rule the registry test will hold in P11 is stated here because it is the
reason the table has this shape: **every notification kind maps to exactly one
group**. A kind with no group would be a message the settings screen cannot
describe, delivered to someone who believes they switched it off.

`push` exists in the model and is not offered on screen — there is no push
channel yet, and a switch for a channel that cannot deliver is the same lie in
the other direction.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction

from apps.audit import services as audit
from apps.common.errors import InvalidState

from ..models import NotificationPreference

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

CHANNELS = ("in_app", "email")

#: Defaults chosen so a new account is told about what affects it and left
#: alone otherwise. Digest and product news are opt-in: an account that has
#: never used the product does not want a weekly summary of nothing.
DEFAULTS: dict[str, dict[str, bool]] = {
    NotificationPreference.Event.JOB_MATCHES: {"in_app": True, "email": True},
    NotificationPreference.Event.APPLICATION_UPDATES: {"in_app": True, "email": True},
    NotificationPreference.Event.EMPLOYER_MESSAGES: {"in_app": True, "email": True},
    NotificationPreference.Event.CV_INSIGHTS: {"in_app": True, "email": False},
    NotificationPreference.Event.WEEKLY_DIGEST: {"in_app": False, "email": True},
    NotificationPreference.Event.PRODUCT_NEWS: {"in_app": False, "email": False},
    # Following a company is asking to hear from it, so it is on — in the app.
    # Email stays off until asked for: a followed company that posts weekly
    # should not fill somebody's inbox by default.
    NotificationPreference.Event.COMPANY_UPDATES: {"in_app": True, "email": False},
}

#: The same keys for both roles — one stored shape, so switching role never
#: silently discards a preference — described from the reader's own side. An
#: employer told that "a role above your threshold is posted" has been handed
#: the candidate's copy and stops trusting the page.
WORDING: dict[str, dict[str, str]] = {
    NotificationPreference.Event.JOB_MATCHES: {
        "candidate_label": "New job matches",
        "candidate_text": "A role scoring above your threshold is posted.",
        "employer_label": "Posting performance",
        "employer_text": "A posting is being seen but not applied to, or is about to close.",
    },
    NotificationPreference.Event.APPLICATION_UPDATES: {
        "candidate_label": "Application updates",
        "candidate_text": "An employer opens, shortlists or declines your application.",
        "employer_label": "New applicants",
        "employer_text": "Someone applies to one of your postings.",
    },
    NotificationPreference.Event.EMPLOYER_MESSAGES: {
        "candidate_label": "Messages from employers",
        "candidate_text": "Someone hiring writes to you directly.",
        "employer_label": "Candidate replies",
        "employer_text": "A candidate answers a message you sent.",
    },
    NotificationPreference.Event.CV_INSIGHTS: {
        "candidate_label": "CV insights",
        "candidate_text": "A new analysis finishes, or a fix is suggested for your CV.",
        "employer_label": "Hiring insights",
        "employer_text": "Suggestions drawn from how your postings are doing.",
    },
    NotificationPreference.Event.WEEKLY_DIGEST: {
        "candidate_label": "Weekly digest",
        # The day is the one `config/celery.py` schedules (Monday 08:00, in
        # TIME_ZONE). It said Sunday while the job ran on Monday.
        "candidate_text": "One email on Monday morning: matches, views and what moved.",
        "employer_label": "Weekly digest",
        "employer_text": "One email on Monday morning: applicants, views and what moved.",
    },
    NotificationPreference.Event.COMPANY_UPDATES: {
        "candidate_label": "Companies you follow",
        "candidate_text": "A company you follow posts a job, is verified, or shares news or an event.",
        "employer_label": "Companies you follow",
        "employer_text": "A company you follow posts a job, is verified, or shares news or an event.",
    },
    NotificationPreference.Event.PRODUCT_NEWS: {
        "candidate_label": "Product news",
        "candidate_text": "New features. Rare, and never a sales email.",
        "employer_label": "Product news",
        "employer_text": "New features. Rare, and never a sales email.",
    },
}


@transaction.atomic
def ensure_defaults(*, user: User) -> list[NotificationPreference]:
    """Every group has a row for this user. Idempotent."""
    existing = {row.event_key: row for row in user.notification_preferences.all()}
    missing = [
        NotificationPreference(user=user, event_key=key, **defaults)
        for key, defaults in DEFAULTS.items()
        if key not in existing
    ]
    if missing:
        NotificationPreference.objects.bulk_create(missing, ignore_conflicts=True)
    return list(user.notification_preferences.all())


def allows(*, user: User, event_key: str, channel: str) -> bool:
    """Is this event allowed down this channel? Read by delivery in P11."""
    if channel not in CHANNELS and channel != "push":
        raise InvalidState("That isn't a delivery channel.")

    row = user.notification_preferences.filter(event_key=event_key).first()
    if row is None:
        # An unconfigured account gets the default rather than silence: a
        # missing row must never mean "send nothing", or a delivery bug reads
        # as a preference.
        return bool(DEFAULTS.get(event_key, {}).get(channel, False))
    return bool(getattr(row, channel))


@transaction.atomic
def set_channel(
    *, user: User, actor: User, event_key: str, channel: str, on: bool
) -> NotificationPreference:
    """One cell of the notifications grid."""
    if event_key not in NotificationPreference.Event.values:
        raise InvalidState("That isn't a notification type.")
    if channel not in CHANNELS:
        raise InvalidState("That isn't a delivery channel.")

    row, _created = NotificationPreference.objects.get_or_create(
        user=user, event_key=event_key, defaults=DEFAULTS.get(event_key, {})
    )
    setattr(row, channel, bool(on))
    row.save(update_fields=[channel, "updated_at"])

    audit.record(
        verb="account.notification_preference_changed",
        actor=actor,
        target=row,
        metadata={"event": event_key, "channel": channel, "on": bool(on)},
    )
    return row


@transaction.atomic
def set_all(*, user: User, actor: User, on: bool) -> None:
    """Everything on, or everything off — the two states people actually want."""
    ensure_defaults(user=user)
    user.notification_preferences.update(in_app=bool(on), email=bool(on))
    audit.record(
        verb="account.notification_preferences_bulk_set",
        actor=actor,
        target=user,
        metadata={"on": bool(on)},
    )


def _clock(value: str) -> str:
    text = (value or "").strip()
    if len(text) != 5 or text[2] != ":":
        raise InvalidState("Use a time like 22:00.")
    try:
        dt.time.fromisoformat(text)
    except ValueError:
        raise InvalidState("Use a time like 22:00.") from None
    return text


@transaction.atomic
def set_quiet_hours(*, user: User, actor: User, start: str, end: str) -> dict:
    """One quiet-hours window for every group, in the person's own timezone (FR-11.4).

    The column has existed on every preference row, and delivery has honoured
    it, with no screen to set it. One window rather than six: nobody wants
    their messages quiet at night but their application updates loud.
    Both blank clears it.
    """
    if not (start or "").strip() and not (end or "").strip():
        window: dict = {}
    else:
        opens, closes = _clock(start), _clock(end)
        if opens == closes:
            raise InvalidState("Quiet hours need a different start and end time.")
        zone = user.timezone or "Asia/Kathmandu"
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = "Asia/Kathmandu"
        window = {"from": opens, "to": closes, "tz": zone}

    ensure_defaults(user=user)
    user.notification_preferences.update(quiet_hours=window)
    audit.record(
        verb="account.quiet_hours_changed",
        actor=actor,
        target=user,
        metadata={"from": window.get("from", ""), "to": window.get("to", "")},
    )
    return window


def quiet_hours_for(user: User) -> dict:
    """The saved window, or an empty dict."""
    row = user.notification_preferences.exclude(quiet_hours={}).first()
    return dict(row.quiet_hours) if row is not None else {}


def role_for(user: User) -> str:
    """Which side's wording a person reads: `candidate`, `employer` or `both`.

    Membership decides the employer side (ADR-A1), a candidate profile the
    other. `WORDING` had an employer column from the start and nothing asked
    for it, so a recruiter was told about "messages from employers" and "new
    job matches".
    """
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import CompanyMembership

    employer = CompanyMembership.objects.active().filter(user=user).exists()
    candidate = CandidateProfile.objects.filter(user=user).exists()
    if employer and candidate:
        return "both"
    return "employer" if employer else "candidate"


def wording_for(key: str, role: str) -> tuple[str, str]:
    """Label and description for one group, from the reader's own side."""
    wording = WORDING[key]
    if role == "employer":
        return wording["employer_label"], wording["employer_text"]
    if role == "both" and wording["candidate_text"] != wording["employer_text"]:
        label = (
            wording["candidate_label"]
            if wording["candidate_label"] == wording["employer_label"]
            else f"{wording['candidate_label']} · {wording['employer_label']}"
        )
        text = (
            f"As a job seeker: {wording['candidate_text']} "
            f"As an employer: {wording['employer_text']}"
        )
        return label, text
    return wording["candidate_label"], wording["candidate_text"]


def rows_for(*, user: User, role: str = "candidate") -> list[dict]:
    """The grid, already joined to the saved values, so the template loops once."""
    saved = {row.event_key: row for row in ensure_defaults(user=user)}

    grid = []
    for key in NotificationPreference.Event.values:
        row = saved[key]
        label, text = wording_for(key, role)
        grid.append(
            {
                "key": key,
                "label": label,
                "text": text,
                "channels": [
                    {"key": "in_app", "label": "In app", "on": row.in_app},
                    {"key": "email", "label": "Email", "on": row.email},
                ],
                "silent": row.is_silent,
            }
        )
    return grid
