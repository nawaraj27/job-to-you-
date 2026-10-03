"""
The repeating parts of a profile: experience, education, projects and the rest.

One module rather than seven, because the operations are genuinely the same
shape — create a row under a profile, edit it, remove it, reorder it — and seven
near-identical files would drift apart within a phase. What differs per section
is *validation*, and that is where the code here is specific.

Sections do not carry their own `row_version`. The guard that matters is
ownership: a stale edit to one experience row cannot silently overwrite another
person's, because the row is fetched through the profile that owns it.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from django.db import models, transaction
from django.db.models import Max

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.taxonomy.models import Language

from ..models import (
    CandidateProfile,
    LinkKind,
    Proficiency,
    ProfileAward,
    ProfileCertification,
    ProfileEducation,
    ProfileExperience,
    ProfileLanguage,
    ProfileLink,
    ProfileProject,
)
from . import profile as profile_service

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

#: name -> (model, related name, writable fields). A section not listed here
#: cannot be written at all, which is what keeps a form post from reaching a
#: field nobody meant to expose.
SECTIONS: dict[str, tuple[type[models.Model], str, tuple[str, ...]]] = {
    "experience": (
        ProfileExperience,
        "experiences",
        ("title", "org_name", "start_date", "end_date", "is_current", "summary", "bullets"),
    ),
    "education": (
        ProfileEducation,
        "education",
        (
            "institution",
            "qualification",
            "field_of_study",
            "start_date",
            "end_date",
            "is_current",
            "summary",
        ),
    ),
    "project": (
        ProfileProject,
        "projects",
        ("name", "url", "summary", "started_on", "finished_on"),
    ),
    "certification": (
        ProfileCertification,
        "certifications",
        ("name", "issuer", "issued_on", "expires_on", "credential_id", "credential_url"),
    ),
    "language": (ProfileLanguage, "languages", ("language_id", "proficiency")),
    "link": (ProfileLink, "links", ("kind", "label", "url")),
    "award": (ProfileAward, "awards", ("name", "issuer", "awarded_on", "summary")),
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "experience": ("title", "org_name", "start_date"),
    "education": ("institution",),
    "project": ("name",),
    "certification": ("name",),
    "language": ("language_id", "proficiency"),
    "link": ("url",),
    "award": ("name",),
}

DATE_FIELDS = frozenset(
    {"start_date", "end_date", "issued_on", "expires_on", "started_on", "finished_on", "awarded_on"}
)
BOOLEAN_FIELDS = frozenset({"is_current"})


@transaction.atomic
def add_entry(*, profile: CandidateProfile, actor: User, section: str, **values: Any) -> object:
    model, related, allowed = _section(section)
    clean = _clean(section, values, allowed)
    _require(section, clean)
    _refuse_duplicates(profile, section, clean)

    next_order = (getattr(profile, related).aggregate(highest=Max("order"))["highest"] or 0) + 1
    entry = model._default_manager.create(profile=profile, order=next_order, **clean)

    _after_change(profile, actor=actor, section=section, verb="added")
    return entry


@transaction.atomic
def update_entry(
    *, profile: CandidateProfile, actor: User, section: str, entry_id: int, **values: Any
) -> object:
    _model, related, allowed = _section(section)
    entry = getattr(profile, related).filter(pk=entry_id).first()
    if entry is None:
        raise NotFound("That entry is not on your profile.")

    clean = _clean(section, values, allowed)
    if not clean:
        raise InvalidState("There was nothing to save.")

    for key, value in clean.items():
        setattr(entry, key, value)
    _require(section, {**_as_dict(entry, allowed), **clean})
    entry.save()

    _after_change(profile, actor=actor, section=section, verb="updated")
    return entry


@transaction.atomic
def remove_entry(*, profile: CandidateProfile, actor: User, section: str, entry_id: int) -> None:
    _model, related, _allowed = _section(section)
    entry = getattr(profile, related).filter(pk=entry_id).first()
    if entry is None:
        raise NotFound("That entry is not on your profile.")

    entry.delete()
    _after_change(profile, actor=actor, section=section, verb="removed")


# --- internals ------------------------------------------------------------


def _refuse_duplicates(profile: CandidateProfile, section: str, clean: dict[str, Any]) -> None:
    """Turn the uniqueness constraints into sentences before they fire.

    The constraint is the real guarantee; this exists so the common case reads
    as an explanation rather than as a 500 from an IntegrityError.
    """
    if section == "link" and profile.links.filter(url=clean.get("url", "")).exists():
        raise Conflict("That link is already on your profile.")
    if section == "language":
        language_id = int(clean.get("language_id") or 0)
        if language_id and profile.languages.filter(language_id=language_id).exists():
            raise Conflict("That language is already on your profile.")


def _section(section: str) -> tuple[type[models.Model], str, tuple[str, ...]]:
    try:
        return SECTIONS[section]
    except KeyError:
        raise InvalidState(f"{section!r} is not a section of a profile.") from None


def _as_dict(entry: object, allowed: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(entry, field) for field in allowed}


def _require(section: str, clean: dict[str, Any]) -> None:
    missing = [field for field in REQUIRED[section] if not clean.get(field)]
    if missing:
        raise InvalidState(f"Fill in {', '.join(missing).replace('_', ' ')}.")


def _clean(section: str, values: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    clean: dict[str, Any] = {}

    for key, value in values.items():
        if key not in allowed:
            continue
        if key in DATE_FIELDS:
            clean[key] = _clean_date(value, key)
        elif key in BOOLEAN_FIELDS:
            clean[key] = value in (True, "on", "true", "True", "1", "yes")
        elif key == "bullets":
            clean[key] = [line.strip() for line in (value or []) if str(line).strip()]
        elif isinstance(value, str):
            clean[key] = value.strip()
        else:
            clean[key] = value

    if section == "experience":
        _clean_experience(clean)
    if section == "education":
        _clean_education(clean)
    if section == "language":
        _clean_language(clean)
    if section == "link":
        _clean_link(clean)
    if section == "certification":
        _clean_certification(clean)

    return clean


def _clean_experience(clean: dict[str, Any]) -> None:
    if clean.get("is_current"):
        # A job you still hold has no end date. Silently keeping one makes
        # every duration calculation ambiguous, and the check constraint
        # would reject the row with a message nobody can act on.
        clean["end_date"] = None
    start, end = clean.get("start_date"), clean.get("end_date")
    if start and end and end < start:
        raise InvalidState("The end date is before the start date.")
    if start and start > dt.date.today():
        raise InvalidState("A role cannot start in the future.")


def _clean_education(clean: dict[str, Any]) -> None:
    if clean.get("is_current"):
        clean["end_date"] = None
    start, end = clean.get("start_date"), clean.get("end_date")
    if start and end and end < start:
        raise InvalidState("The end date is before the start date.")


def _clean_certification(clean: dict[str, Any]) -> None:
    issued, expires = clean.get("issued_on"), clean.get("expires_on")
    if issued and expires and expires < issued:
        raise InvalidState("A certificate cannot expire before it was issued.")


def _clean_language(clean: dict[str, Any]) -> None:
    if "proficiency" in clean and clean["proficiency"] not in Proficiency.values:
        raise InvalidState("That isn't a proficiency level.")
    if clean.get("language_id") and not Language.objects.filter(pk=clean["language_id"]).exists():
        raise InvalidState("That isn't a language we know.")


def _clean_link(clean: dict[str, Any]) -> None:
    if "kind" in clean and clean["kind"] not in LinkKind.values:
        clean["kind"] = LinkKind.OTHER

    url = clean.get("url")
    if url is None:
        return

    if "://" not in url:
        # Someone typing "github.com/name" means https. Rejecting it teaches
        # them to type the scheme; adding it teaches them nothing and works.
        url = f"https://{url}"
    if not url.startswith(("http://", "https://")) or "." not in url.split("//", 1)[-1]:
        raise InvalidState("That doesn't look like a web address.")
    clean["url"] = url


def _clean_date(value: Any, field: str) -> dt.date | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise InvalidState(
            f"{field.replace('_', ' ').capitalize()} needs to be a date, like 2024-06-01."
        ) from None


def _after_change(profile: CandidateProfile, *, actor: User, section: str, verb: str) -> None:
    profile_service.recompute(profile)
    audit.record(
        verb=f"candidate.section_{verb}",
        actor=actor,
        target=profile,
        metadata={"section": section},
    )
