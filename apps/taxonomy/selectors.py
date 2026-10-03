"""Taxonomy reads. Reference data is public — no tenant scoping applies."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from .models import Industry, JobCategory, Language, Location, Skill
from .normalisation import normalise


def active_skills() -> QuerySet[Skill]:
    """Skills that may be attached to new records.

    Retired skills are excluded from every offer surface but stay readable, so
    a profile written three years ago still says what its author meant.
    """
    return Skill.objects.active().select_related("category")


def skills_in(category_slug: str) -> QuerySet[Skill]:
    return active_skills().filter(category__slug=category_slug)


def search_skills(term: str, *, limit: int = 20) -> QuerySet[Skill]:
    """Autocomplete. Matches the canonical name or any alias.

    Aliases are included because someone typing "JS" should see JavaScript
    offered, not an empty list that pushes them to invent a duplicate.
    """
    key = normalise(term)
    if not key:
        return Skill.objects.none()

    return (
        active_skills()
        .filter(Q(name__icontains=key) | Q(aliases__normalised__startswith=key))
        .distinct()[:limit]
    )


def skills_by_external_id(source: str, external_ids: list[str]) -> dict[str, Skill]:
    """Look skills up by their identity in an external taxonomy."""
    from .models import SkillExternalId

    rows = SkillExternalId.objects.select_related("skill").filter(
        source=source, external_id__in=external_ids
    )
    return {row.external_id: row.skill for row in rows}


def job_categories() -> QuerySet[JobCategory]:
    return JobCategory.objects.select_related("parent").all()


def industries() -> QuerySet[Industry]:
    return Industry.objects.all()


def languages() -> QuerySet[Language]:
    return Language.objects.all()


def locations_in(country: str) -> QuerySet[Location]:
    return Location.objects.filter(country=country.upper())


def skill_names(ids) -> dict[int, str]:
    """`{id: name}` for a set of skill ids. Used to label a match breakdown."""
    from .models import Skill

    wanted = list(ids)
    if not wanted:
        return {}
    return dict(Skill.objects.filter(pk__in=wanted).values_list("pk", "name"))
