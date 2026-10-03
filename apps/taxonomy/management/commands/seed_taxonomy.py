"""
Load the curated taxonomy seed.

Idempotent: running it twice changes nothing, which matters because it runs on
every fresh environment and nobody should have to remember whether it already
did. Existing rows are updated in place rather than duplicated, so a curator's
edits to a name survive a re-seed while structure stays correct.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.taxonomy import seeds
from apps.taxonomy.models import (
    Industry,
    JobCategory,
    Language,
    Location,
    Provenance,
    RelationKind,
    Skill,
    SkillAlias,
    SkillCategory,
    SkillRelation,
    SkillTranslation,
)
from apps.taxonomy.normalisation import normalise
from apps.taxonomy.services import skills as skill_service


class Command(BaseCommand):
    help = "Load the curated skill, category, location and language seed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet-summary",
            action="store_true",
            help="Suppress the per-model counts, for use in test setup.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        counts = {
            "skill categories": self._skill_categories(),
            "skills": self._skills(),
            "aliases": self._aliases(),
            "translations": self._translations(),
            "relations": self._relations(),
            "hierarchy edges": self._hierarchy(),
            "deprecated skills": self._deprecated(),
            "job categories": self._job_categories(),
            "industries": self._industries(),
            "locations": self._locations(),
            "languages": self._languages(),
        }

        if not options["quiet_summary"]:
            for label, count in counts.items():
                self.stdout.write(f"{label}: {count}")

    def _skill_categories(self) -> int:
        for slug, name, _parent in seeds.SKILL_CATEGORIES:
            SkillCategory.objects.update_or_create(slug=slug, defaults={"name": name})
        # Parents in a second pass, so declaration order does not matter.
        for slug, _name, parent_slug in seeds.SKILL_CATEGORIES:
            if parent_slug:
                SkillCategory.objects.filter(slug=slug).update(
                    parent=SkillCategory.objects.get(slug=parent_slug)
                )
        return SkillCategory.objects.count()

    def _skills(self) -> int:
        categories = {c.slug: c for c in SkillCategory.objects.all()}
        for slug, name, category_slug, _aliases in seeds.SKILLS:
            Skill.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "category": categories.get(category_slug),
                    "kind": _kind_for(slug, category_slug),
                },
            )
        return Skill.objects.count()

    def _aliases(self) -> int:
        for slug, _name, _category, aliases in seeds.SKILLS:
            skill = Skill.objects.get(slug=slug)
            for alias in aliases:
                key = normalise(alias)
                existing = SkillAlias.objects.filter(normalised=key, locale="en").first()
                if existing is None:
                    SkillAlias.objects.create(
                        skill=skill,
                        alias=alias,
                        locale="en",
                        source=Provenance.CURATED,
                        created_reason="Curated seed.",
                    )
                elif existing.skill_id != skill.pk:
                    # Two seed skills claiming one alias is a data bug, not a
                    # runtime condition — fail loudly rather than pick a winner.
                    raise ValueError(
                        f"Alias {alias!r} is claimed by both {existing.skill.slug} and {slug}."
                    )
        return SkillAlias.objects.count()

    def _translations(self) -> int:
        for slug, locale, name in seeds.SKILL_TRANSLATIONS:
            SkillTranslation.objects.update_or_create(
                skill=Skill.objects.get(slug=slug), locale=locale, defaults={"name": name}
            )
        return SkillTranslation.objects.count()

    def _hierarchy(self) -> int:
        """Both directions, so ancestors and descendants each read one index."""
        for child_slug, parent_slug in seeds.SKILL_HIERARCHY:
            child = Skill.objects.get(slug=child_slug)
            parent = Skill.objects.get(slug=parent_slug)
            provenance = {"source": Provenance.CURATED, "created_reason": "Curated seed."}
            SkillRelation.objects.get_or_create(
                from_skill=child, to_skill=parent, kind=RelationKind.BROADER, defaults=provenance
            )
            SkillRelation.objects.get_or_create(
                from_skill=parent, to_skill=child, kind=RelationKind.NARROWER, defaults=provenance
            )
        return SkillRelation.objects.filter(
            kind__in=[RelationKind.BROADER, RelationKind.NARROWER]
        ).count()

    def _deprecated(self) -> int:
        """Retired skills, created then deprecated through the service.

        Written this way rather than by setting `status` directly so the seed
        exercises the same guard rails a curator does — and so re-seeding an
        already-retired skill is a no-op rather than an error.
        """
        categories = {c.slug: c for c in SkillCategory.objects.all()}

        for slug, name, category_slug, replacement_slug, reason in seeds.DEPRECATED_SKILLS:
            skill, _ = Skill.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "category": categories.get(category_slug),
                    "kind": _kind_for(slug, category_slug),
                },
            )
            if not skill.is_active:
                continue
            replacement = Skill.objects.get(slug=replacement_slug) if replacement_slug else None
            skill_service.deprecate(skill=skill, reason=reason, replacement=replacement)

        return Skill.objects.retired().count()

    def _relations(self) -> int:
        for from_slug, to_slug, kind, weight in seeds.SKILL_RELATIONS:
            SkillRelation.objects.update_or_create(
                from_skill=Skill.objects.get(slug=from_slug),
                to_skill=Skill.objects.get(slug=to_slug),
                kind=kind,
                defaults={
                    "weight": Decimal(weight),
                    "source": Provenance.CURATED,
                    "created_reason": "Curated seed.",
                },
            )
        return SkillRelation.objects.count()

    def _job_categories(self) -> int:
        for slug, name, _parent in seeds.JOB_CATEGORIES:
            JobCategory.objects.update_or_create(slug=slug, defaults={"name": name})
        for slug, _name, parent_slug in seeds.JOB_CATEGORIES:
            if parent_slug:
                JobCategory.objects.filter(slug=slug).update(
                    parent=JobCategory.objects.get(slug=parent_slug)
                )
        return JobCategory.objects.count()

    def _industries(self) -> int:
        for slug, name in seeds.INDUSTRIES:
            Industry.objects.update_or_create(slug=slug, defaults={"name": name})
        return Industry.objects.count()

    def _locations(self) -> int:
        for country, region, city, timezone in seeds.LOCATIONS:
            Location.objects.update_or_create(
                country=country, region=region, city=city, defaults={"timezone": timezone}
            )
        return Location.objects.count()

    def _languages(self) -> int:
        for code, name in seeds.LANGUAGES:
            Language.objects.update_or_create(code=code, defaults={"name": name})
        return Language.objects.count()


def _kind_for(slug: str, category_slug: str) -> str:
    """A skill's kind: its own if it differs, otherwise its category's default."""
    return seeds.SKILL_KINDS.get(slug) or seeds.CATEGORY_KINDS.get(category_slug, "unknown")
