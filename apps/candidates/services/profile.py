"""
The candidate profile itself: creation, the editable groups, privacy.

Three rules hold across everything here, and they are the reason these are
services rather than form `save()` calls:

  * **Completeness and searchability are derived on every write.** A stored
    number describing data that changes is a number that goes stale, and the
    version of this screen that hard-coded 82% proved it.
  * **Salary is normalised at write time with its provenance** (ADR-A39). At
    read time the rate would have moved, and last March's match would stop
    being explicable.
  * **`row_version` guards the second tab.** Two tabs on one profile is normal
    behaviour, and a lost update there is silent — the user sees their own tab,
    which still shows what they typed.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from django.db import transaction

from apps.audit import services as audit
from apps.common.errors import InvalidState, StaleWrite
from apps.notifications import events
from apps.taxonomy.models import Location
from apps.taxonomy.services import money

from .. import completeness as completeness_rules
from ..models import CandidateProfile, Seeking, Visibility, WorkType

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

logger = logging.getLogger(__name__)

#: Editable groups. Declared as data so a view cannot write a field simply by
#: naming it in a form post — anything not listed here is dropped, not stored.
BASICS = ("headline", "summary", "phone", "location_id")
SEEKING = (
    "seeking",
    "notice_period_days",
    "open_to",
    "salary_min",
    "salary_max",
    "currency",
    "period",
)
PRIVACY = ("visibility", "allow_contact", "show_salary", "search_indexing")


def get_or_create_profile(*, user: User) -> CandidateProfile:
    """The candidate's profile, created on first use.

    Created lazily rather than at registration because an employer account
    never needs one, and a row that exists for every user makes "is this person
    a candidate" a question with two answers.

    The lookup is outside the transaction and the creation inside it. Every
    candidate screen calls this on every render and all but the first of them
    find the row, so wrapping the whole function opened a transaction hundreds
    of times a session in order to read one row and write nothing.
    """
    profile = CandidateProfile.objects.filter(user=user).first()
    if profile is not None:
        return profile

    return _create_profile(user=user)


@transaction.atomic
def _create_profile(*, user: User) -> CandidateProfile:
    profile = CandidateProfile.objects.create(user=user)
    events.emit(
        "candidate.profile_created",
        profile,
        payload={"profile_public_id": str(profile.public_id), "user_id": user.pk},
    )
    audit.record(verb="candidate.profile_created", actor=user, target=profile)
    return profile


@transaction.atomic
def update_basics(
    *,
    profile: CandidateProfile,
    actor: User,
    expected_version: int | None = None,
    **values: Any,
) -> CandidateProfile:
    """Name-adjacent fields: headline, summary, phone, location."""
    _guard_version(profile, expected_version)
    clean = _only(values, BASICS)

    if clean.get("location_id") and not Location.objects.filter(pk=clean["location_id"]).exists():
        raise InvalidState("That location is not one we know.")

    if "phone" in clean:
        clean["phone"] = _clean_phone(clean["phone"])

    return _apply(profile, clean, actor=actor, section="basics")


@transaction.atomic
def update_seeking(
    *,
    profile: CandidateProfile,
    actor: User,
    expected_version: int | None = None,
    **values: Any,
) -> CandidateProfile:
    """What the candidate wants: availability, work types, salary."""
    _guard_version(profile, expected_version)
    clean = _only(values, SEEKING)

    if clean.get("seeking") and clean["seeking"] not in Seeking.values:
        raise InvalidState("That isn't an availability option.")

    if "open_to" in clean:
        clean["open_to"] = _clean_work_types(clean["open_to"])

    for key in ("salary_min", "salary_max"):
        if key in clean:
            clean[key] = _clean_amount(clean[key], label=key)

    if "notice_period_days" in clean:
        clean["notice_period_days"] = _clean_notice(clean["notice_period_days"])

    merged = {**_current_salary(profile), **clean}
    low, high = merged["salary_min"], merged["salary_max"]
    if low is not None and high is not None and high < low:
        raise InvalidState("The top of the range cannot be below the bottom.")

    profile = _apply(profile, clean, actor=actor, section="seeking", commit=False)
    _normalise_salary(profile)
    _finish(profile, actor=actor, sections=["seeking"])
    return profile


@transaction.atomic
def update_privacy(
    *,
    profile: CandidateProfile,
    actor: User,
    expected_version: int | None = None,
    **values: Any,
) -> CandidateProfile:
    """Who can see this profile, and what they see."""
    _guard_version(profile, expected_version)
    clean = _only(values, PRIVACY)

    was = profile.visibility
    if "visibility" in clean and clean["visibility"] not in Visibility.values:
        raise InvalidState("That isn't a visibility option.")

    for key in ("allow_contact", "show_salary", "search_indexing"):
        if key in clean:
            clean[key] = _clean_bool(clean[key])

    # Indexing only means anything on a public profile, and leaving it set
    # while visibility drops is how a "private" profile ends up in a search
    # engine the next time it goes public.
    if clean.get("visibility") and clean["visibility"] != Visibility.EVERYONE:
        clean["search_indexing"] = False

    profile = _apply(profile, clean, actor=actor, section="privacy", commit=False)
    _finish(profile, actor=actor, sections=["privacy"])

    if profile.visibility != was:
        if _BREADTH[profile.visibility] < _BREADTH[was]:
            _withdraw_employer_access(profile, actor=actor, was=was)
        events.emit(
            "candidate.visibility_changed",
            profile,
            payload={
                "profile_public_id": str(profile.public_id),
                "from_visibility": was,
                "to_visibility": profile.visibility,
            },
        )
        audit.record(
            verb="candidate.visibility_changed",
            actor=actor,
            target=profile,
            metadata={"from": was, "to": profile.visibility},
        )
    return profile


#: How widely each setting lets employers in. Moving down this scale is
#: narrowing, and narrowing withdraws access (decision D-06).
_BREADTH: dict[str, int] = {
    Visibility.EVERYONE: 2,
    Visibility.EMPLOYERS: 1,
    Visibility.NOBODY: 0,
}

#: Grants the company holds by its own act or by the profile being public.
#: Narrowing revokes them; an `application` grant is the candidate's own act
#: towards that company and stays (D-06).
NARROWING_REVOKES = ("public", "pool", "invited")


def _withdraw_employer_access(profile: CandidateProfile, *, actor: User, was: str) -> None:
    """Revoke what employers hold beyond the candidate's own applications.

    Decision D-06. Opening a public profile creates a `public` grant; saving
    someone to a talent pool creates a `pool` grant; inviting them creates an
    `invited` grant. All three used to outlive "employers you've applied to" and
    "nobody", so a company kept finding and reading a candidate who had just
    said they should not. They are revoked, the pool memberships behind them are
    removed, and each company's revocation is audited with the reason.
    `application` grants stay: they are the candidate's own relationship with
    that company, and the application record and what was sent with it remain
    readable to the team that received it.
    """
    from django.utils import timezone as django_timezone

    from apps.applications.models import CandidateVisibilityGrant, TalentPoolMember

    now = django_timezone.now()
    live = CandidateVisibilityGrant.objects.live().filter(
        candidate=profile, basis__in=NARROWING_REVOKES
    )
    by_company: dict[int, dict[str, int]] = {}
    for company_id, basis in live.values_list("company_id", "basis"):
        bases = by_company.setdefault(company_id, {})
        bases[basis] = bases.get(basis, 0) + 1
    live.update(revoked_at=now)

    pooled = TalentPoolMember.objects.filter(candidate=profile, deleted_at__isnull=True)
    pools_by_company: dict[int, int] = {}
    for company_id in pooled.values_list("pool__company_id", flat=True):
        pools_by_company[company_id] = pools_by_company.get(company_id, 0) + 1
    pooled.update(deleted_at=now)

    from apps.companies.models import Company

    companies = Company.objects.in_bulk(set(by_company) | set(pools_by_company))
    for company_id in sorted(companies):
        audit.record(
            verb="candidate.visibility_revoked",
            actor=actor,
            target=profile,
            company=companies[company_id],
            metadata={
                "reason": "visibility_narrowed",
                "from": was,
                "to": profile.visibility,
                "bases": by_company.get(company_id, {}),
                "grants": sum(by_company.get(company_id, {}).values()),
                "pool_memberships": pools_by_company.get(company_id, 0),
            },
        )


def recompute(profile: CandidateProfile) -> CandidateProfile:
    """Refresh the derived fields after a child row changed.

    Called by the section and skill services rather than by a signal: a save
    that silently triggers other saves is exactly what DEVELOPER.md §6.5 bans,
    because it makes the write path impossible to read.
    """
    score, _gaps = completeness_rules.evaluate(profile)
    searchable = (
        score >= completeness_rules.SEARCHABLE_THRESHOLD and profile.visibility != Visibility.NOBODY
    )
    months = experience_months(profile)

    if (
        score != profile.completeness
        or searchable != profile.is_searchable
        or months != profile.experience_months
    ):
        profile.completeness = score
        profile.is_searchable = searchable
        profile.experience_months = months
        profile.save(
            update_fields=["completeness", "is_searchable", "experience_months", "updated_at"]
        )
    return profile


def experience_months(profile: CandidateProfile) -> int:
    """Distinct months of work history, overlaps counted once.

    One implementation, shared with the CV analyser and the matcher through
    `apps.common.periods` (P12, P13). Somebody who freelanced while employed has
    not worked eleven years in five, and three answers to "how long did this
    person work" would be two too many.
    """
    from django.utils import timezone as django_timezone

    from apps.common import periods

    today = django_timezone.localdate()
    found = [
        periods.Period(
            start=row.start_date,
            end=today if row.is_current or row.end_date is None else row.end_date,
            is_current=row.is_current,
        )
        for row in profile.experiences.all()
        if row.start_date
    ]
    return periods.total_months(found)


def gaps(profile: CandidateProfile) -> list[str]:
    """What is still missing, most valuable first."""
    _score, missing = completeness_rules.evaluate(profile)
    return missing


# --- internals ------------------------------------------------------------


def _only(values: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in allowed}


def _guard_version(profile: CandidateProfile, expected: int | None) -> None:
    if expected is not None and expected != profile.row_version:
        raise StaleWrite(
            "This profile was changed in another tab or window. "
            "Reload to see the current version before saving again."
        )


def _apply(
    profile: CandidateProfile,
    clean: dict[str, Any],
    *,
    actor: User,
    section: str,
    commit: bool = True,
) -> CandidateProfile:
    if not clean:
        raise InvalidState("There was nothing to save.")

    for key, value in clean.items():
        setattr(profile, key, value)

    if commit:
        _finish(profile, actor=actor, sections=[section])
    return profile


def _finish(profile: CandidateProfile, *, actor: User, sections: list[str]) -> None:
    """One write path: version, derived fields, save, event, audit."""
    profile.row_version += 1
    score, _ = completeness_rules.evaluate(profile)
    profile.completeness = score
    profile.is_searchable = (
        score >= completeness_rules.SEARCHABLE_THRESHOLD and profile.visibility != Visibility.NOBODY
    )
    profile.save()

    events.emit(
        "candidate.profile_updated",
        profile,
        payload={"profile_public_id": str(profile.public_id), "sections": sections},
    )
    audit.record(
        verb="candidate.profile_updated",
        actor=actor,
        target=profile,
        metadata={"sections": sections},
    )


def _current_salary(profile: CandidateProfile) -> dict[str, Decimal | None]:
    return {"salary_min": profile.salary_min, "salary_max": profile.salary_max}


def _normalise_salary(profile: CandidateProfile) -> None:
    """Annualise into the base currency, keeping the rate that did it.

    A missing or stale FX rate is not a reason to refuse the candidate's own
    salary expectation — the figures they typed are the ones the interface
    shows, and they are stored either way. What is lost is comparability, so
    the base columns are cleared rather than left holding a figure produced by
    some earlier rate.
    """
    if (
        not profile.currency
        or not profile.period
        or (profile.salary_min is None and profile.salary_max is None)
    ):
        _clear_base(profile)
        return

    try:
        low = money.normalise_salary(
            amount=profile.salary_min, currency=profile.currency, period=profile.period
        )
        high = money.normalise_salary(
            amount=profile.salary_max, currency=profile.currency, period=profile.period
        )
    except InvalidState as exc:
        logger.warning("salary not normalised for profile %s: %s", profile.public_id, exc.message)
        _clear_base(profile)
        return

    reference = low or high
    if reference is None:  # pragma: no cover - both amounts were None
        _clear_base(profile)
        return

    profile.salary_min_base = low.amount if low else None
    profile.salary_max_base = high.amount if high else None
    profile.fx_rate_id = reference.fx_rate_id
    profile.fx_rate_value = reference.fx_rate_value
    profile.fx_as_of = reference.fx_as_of
    profile.fx_source = reference.fx_source


def _clear_base(profile: CandidateProfile) -> None:
    profile.salary_min_base = None
    profile.salary_max_base = None
    profile.fx_rate_id = None
    profile.fx_rate_value = None
    profile.fx_as_of = None
    profile.fx_source = ""


def _clean_amount(value: Any, *, label: str) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise InvalidState("Type a salary as a number, without symbols.") from None
    if amount < 0:
        raise InvalidState("A salary cannot be negative.")
    if amount >= Decimal("10000000000"):
        raise InvalidState("That salary is larger than this field can hold.")
    return amount.quantize(Decimal("0.01"))


def _clean_notice(value: Any) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        days = int(str(value).strip())
    except ValueError:
        raise InvalidState("Type a notice period as a number of days.") from None
    if days < 0 or days > 365:
        raise InvalidState("A notice period is between 0 and 365 days.")
    return days


def _clean_work_types(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    chosen = [value for value in (values or []) if value in WorkType.values]
    # Deduplicated, order preserved: the checkbox group can post the same value
    # twice, and a list with "remote" in it twice reads as a data bug later.
    return list(dict.fromkeys(chosen))


def _clean_bool(value: Any) -> bool:
    return value in (True, "on", "true", "True", "1", "yes")


def _clean_phone(value: Any) -> str:
    phone = str(value or "").strip()
    if not phone:
        return ""
    digits = [character for character in phone if character.isdigit()]
    if len(digits) < 6 or len(digits) > 15:
        raise InvalidState("That doesn't look like a phone number.")
    return phone[:40]
