"""
Account preferences: appearance, accessibility, locale, timezone.

Written as a service rather than a form save for one reason that shows up
immediately: the locale list is not "every language Django knows", it is "every
language this interface has actually been translated into". Offering the rest
and ignoring the choice is the behaviour the settings screen was explicitly
built to avoid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import transaction

from apps.audit import services as audit
from apps.common.errors import InvalidState

from ..models import UserPreferences

if TYPE_CHECKING:  # pragma: no cover
    from ..models import User

#: Locales the interface exists in. Everything else is shown disabled with the
#: reason, never offered and silently dropped.
AVAILABLE_LOCALES = ("en",)

#: Offered in the picker. Kept short and regional rather than exhaustive: a
#: 400-entry select is a worse experience than six correct options and a search
#: when the list ever needs to grow.
TIMEZONES = (
    "Asia/Kathmandu",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Europe/London",
    "America/New_York",
    "UTC",
)

APPEARANCE_FIELDS = (
    "theme",
    "reduce_motion",
    "larger_text",
    "underline_links",
    "high_contrast",
)
ACCOUNT_FIELDS = ("locale", "timezone")
PRIVACY_FIELDS = ("share_analytics",)

BOOLEAN_FIELDS = frozenset(
    {"reduce_motion", "larger_text", "underline_links", "high_contrast", "share_analytics"}
)

#: (field, css class). The class is the whole implementation — the CSS lives in
#: the design system, so nothing here needs to know what "larger text" means in
#: pixels.
ACCESSIBILITY_CLASSES = (
    ("reduce_motion", "prefs-reduce-motion"),
    ("larger_text", "prefs-larger-text"),
    ("underline_links", "prefs-underline-links"),
    ("high_contrast", "prefs-high-contrast"),
)


def get_or_create(*, user: User) -> UserPreferences:
    preferences, _created = UserPreferences.objects.get_or_create(user=user)
    return preferences


def document_classes(preferences: UserPreferences | None) -> str:
    """Accessibility classes for `<html>`.

    Server-rendered rather than applied by script on load, so the preference is
    in effect on the first paint instead of arriving a frame later as a visible
    jump.
    """
    if preferences is None:
        return ""
    return " ".join(
        css_class for field, css_class in ACCESSIBILITY_CLASSES if getattr(preferences, field)
    )


@transaction.atomic
def update(
    *, preferences: UserPreferences, actor: User, group: str, **values: Any
) -> UserPreferences:
    allowed = {
        "appearance": APPEARANCE_FIELDS,
        "account": ACCOUNT_FIELDS,
        "privacy": PRIVACY_FIELDS,
    }.get(group)
    if allowed is None:
        raise InvalidState("That isn't a settings group.")

    clean = {key: value for key, value in values.items() if key in allowed}
    if not clean:
        raise InvalidState("There was nothing to save.")

    for key in BOOLEAN_FIELDS & clean.keys():
        clean[key] = clean[key] in (True, "on", "true", "True", "1", "yes")

    if "theme" in clean and clean["theme"] not in UserPreferences.Theme.values:
        raise InvalidState("That isn't a theme.")

    if "locale" in clean and clean["locale"] not in AVAILABLE_LOCALES:
        raise InvalidState("That language isn't available yet.")

    if "timezone" in clean and clean["timezone"] not in TIMEZONES:
        raise InvalidState("That isn't a timezone we offer.")

    for key, value in clean.items():
        setattr(preferences, key, value)
    preferences.save(update_fields=[*clean, "updated_at"])

    # The user's own locale and timezone live on `User` too, because email and
    # scheduled jobs have no request to read a preference row from.
    _mirror_to_user(preferences, clean)

    audit.record(
        verb="account.preferences_updated",
        actor=actor,
        target=preferences,
        metadata={"group": group, "fields": sorted(clean)},
    )
    return preferences


@transaction.atomic
def toggle(*, preferences: UserPreferences, actor: User, field: str) -> UserPreferences:
    """Flip one switch. What a control that saves on change does."""
    if field not in BOOLEAN_FIELDS:
        raise InvalidState("That isn't a switch.")
    return update(
        preferences=preferences,
        actor=actor,
        group=_group_of(field),
        **{field: not getattr(preferences, field)},
    )


def _group_of(field: str) -> str:
    if field in PRIVACY_FIELDS:
        return "privacy"
    return "appearance"


def _mirror_to_user(preferences: UserPreferences, clean: dict[str, Any]) -> None:
    fields = []
    if "locale" in clean:
        preferences.user.locale = clean["locale"]
        fields.append("locale")
    if "timezone" in clean:
        preferences.user.timezone = clean["timezone"]
        fields.append("timezone")
    if fields:
        preferences.user.save(update_fields=fields)


def language_choices() -> list[dict[str, Any]]:
    """Every language the roadmap has, with the ones that exist marked.

    Listing the unavailable ones disabled is deliberate: hiding them makes the
    product look monolingual, and offering them makes the control lie.
    """
    return [
        {"key": "en", "label": "English", "available": "en" in AVAILABLE_LOCALES},
        {"key": "ne", "label": "नेपाली (Nepali)", "available": "ne" in AVAILABLE_LOCALES},
        {"key": "hi", "label": "हिन्दी (Hindi)", "available": "hi" in AVAILABLE_LOCALES},
    ]


def default_timezone() -> str:
    return getattr(settings, "TIME_ZONE", "UTC")
