"""
The candidate's skills — the table the matcher reads (SRS §5.4).

Everything here exists to keep one rule true: **a skill on a profile is a row
in the taxonomy, never a string.** Free text would make "Python", "python3" and
"Pyhton" three different requirements, and the failure would be invisible —
scores would simply be wrong, with nothing on screen to say so.

So input is resolved through `taxonomy.services.skills.resolve` and refused if
it does not land. Refusing is a real cost: a candidate whose skill is genuinely
missing from the taxonomy is told no. The alternative — creating a skill row per
typo — costs the matcher its meaning, permanently, for everyone. The unresolved
term is surfaced to the caller so the interface can offer the near matches it
did find — and it is written to the unresolved-term ledger, where a curator
turns real gaps into real skills (ADR-A46). Refusing without recording would
throw away the only evidence that a skill is missing.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from django.db import transaction

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.notifications import events
from apps.taxonomy import selectors as taxonomy_selectors
from apps.taxonomy.models import Skill, TermSource
from apps.taxonomy.services import skills as skill_resolver
from apps.taxonomy.services import terms as term_ledger

from ..models import CandidateProfile, CandidateSkill, SkillLevel, SkillSource
from . import profile as profile_service

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

#: How many skills one profile may claim. Not a storage limit — a profile
#: listing sixty skills is claiming none of them, and the matcher would spread
#: its evidence across all sixty.
MAX_SKILLS = 40


class UnknownSkill(InvalidState):
    """The text did not resolve to a skill in the taxonomy."""

    code = "unknown_skill"
    default_message = "We don't have that skill yet."


def add_skill(
    *,
    profile: CandidateProfile,
    actor: User,
    text: str,
    years: Any = None,
    level: Any = None,
    source: str = SkillSource.SELF,
    locale: str = "en",
) -> CandidateSkill:
    """Resolve free text to a taxonomy skill and attach it.

    Deliberately not one transaction end to end: the unresolved-term ledger
    write has to survive the refusal that follows it, and a single atomic block
    would roll the record back with the exception — throwing away the only
    evidence that a skill is missing (ADR-A46). Resolution happens first, the
    ledger write commits on its own, and only the mutation is transactional.
    """
    text = (text or "").strip()
    if not text:
        raise InvalidState("Type a skill first.")

    if profile.skills.count() >= MAX_SKILLS:
        raise InvalidState(
            f"A profile holds up to {MAX_SKILLS} skills. Remove one before adding another."
        )

    match = skill_resolver.resolve(text, locale=locale)
    if match is None:
        # Recorded before refusing (ADR-A46). The refusal is right; losing the
        # term is not — it is the only evidence that this skill is missing, and
        # nothing can reconstruct it later.
        term_ledger.record(text, source=TermSource.PROFILE, locale=locale)

        suggestions = [skill.name for skill in taxonomy_selectors.search_skills(text, limit=5)]
        raise UnknownSkill(
            f"We don't have {text!r} in our skill list yet.",
            details={"term": text, "suggestions": suggestions},
        )

    if profile.skills.filter(skill=match.skill).exists():
        raise Conflict(f"{match.skill.name} is already on your profile.")

    with transaction.atomic():
        record = CandidateSkill.objects.create(
            profile=profile,
            skill=match.skill,
            years=_clean_years(years),
            level=_clean_level(level),
            source=source if source in SkillSource.values else SkillSource.SELF,
            evidence_ref={"matched_on": text, "method": match.method},
            order=profile.skills.count(),
        )
        _after_change(profile, actor=actor, added=[match.skill.slug], removed=[])

    return record


@transaction.atomic
def remove_skill(*, profile: CandidateProfile, actor: User, skill_id: int) -> None:
    record = profile.skills.select_related("skill").filter(skill_id=skill_id).first()
    if record is None:
        raise NotFound("That skill is not on your profile.")

    slug = record.skill.slug
    record.delete()
    _after_change(profile, actor=actor, added=[], removed=[slug])


@transaction.atomic
def update_skill(
    *,
    profile: CandidateProfile,
    actor: User,
    skill_id: int,
    years: Any = None,
    level: Any = None,
) -> CandidateSkill:
    """Change the claim about a skill already on the profile."""
    record = profile.skills.select_related("skill").filter(skill_id=skill_id).first()
    if record is None:
        raise NotFound("That skill is not on your profile.")

    record.years = _clean_years(years)
    record.level = _clean_level(level)
    record.save(update_fields=["years", "level", "updated_at"])

    audit.record(
        verb="candidate.skill_updated",
        actor=actor,
        target=profile,
        metadata={"skill": record.skill.slug},
    )
    return record


@transaction.atomic
def reorder_skills(*, profile: CandidateProfile, actor: User, skill_ids: list[int]) -> None:
    """Put the skills in the order the candidate wants them read.

    Order is presentation, not weight — the matcher does not read it. Saying so
    here is cheaper than a future reader assuming the first skill scores highest.
    """
    records = {record.skill_id: record for record in profile.skills.all()}
    unknown = [skill_id for skill_id in skill_ids if skill_id not in records]
    if unknown:
        raise NotFound("That skill is not on your profile.")

    for position, skill_id in enumerate(skill_ids):
        record = records[skill_id]
        record.order = position
    CandidateSkill.objects.bulk_update(records.values(), ["order"])

    audit.record(verb="candidate.skills_reordered", actor=actor, target=profile)


def suggestions_for(profile: CandidateProfile, *, limit: int = 6) -> list[Skill]:
    """Skills worth offering next, from what the profile already claims.

    Uses `related` edges only. Implication edges would suggest Python to
    someone who listed Django — technically true, useless as a prompt, and it
    would fill the profile with skills the matcher already credits.
    """
    claimed = set(profile.skills.values_list("skill_id", flat=True))
    if not claimed:
        return []

    seen: dict[int, Skill] = {}
    for record in profile.skills.select_related("skill"):
        for candidate in skill_resolver.related(record.skill):
            if candidate.pk not in claimed and candidate.is_active:
                seen.setdefault(candidate.pk, candidate)
    return list(seen.values())[:limit]


# --- internals ------------------------------------------------------------


def _after_change(
    profile: CandidateProfile, *, actor: User, added: list[str], removed: list[str]
) -> None:
    profile_service.recompute(profile)
    events.emit(
        "candidate.skills_changed",
        profile,
        payload={
            "profile_public_id": str(profile.public_id),
            "added": added,
            "removed": removed,
        },
    )
    audit.record(
        verb="candidate.skills_changed",
        actor=actor,
        target=profile,
        metadata={"added": added, "removed": removed},
    )


def _clean_years(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        years = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise InvalidState("Type years of experience as a number.") from None
    if years < 0 or years > 60:
        raise InvalidState("Years of experience is between 0 and 60.")
    return years.quantize(Decimal("0.1"))


def _clean_level(value: Any) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        level = int(str(value).strip())
    except ValueError:
        raise InvalidState("That isn't a proficiency level.") from None
    if level not in SkillLevel.values:
        raise InvalidState("That isn't a proficiency level.")
    return level
