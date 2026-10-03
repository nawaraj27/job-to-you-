"""
Skill resolution, traversal and lifecycle (ADR-A13).

Resolution is layered, strongest signal first:

  1. exact alias match on the normalised form  — "JS" is JavaScript, full stop
  2. exact canonical name match
  3. exact translation match, for non-English input
  4. trigram similarity, above a threshold

The order is the whole design. "Java" and "JavaScript" are three characters
apart, so a fuzzy match allowed to outrank an exact one turns a candidate's Java
experience into JavaScript, silently, on every job they are scored against. The
trigram pass exists for typos, not for near-neighbours.

Resolution always returns the skill **in service today**: a retired skill
forwards through its replacement, so a profile written three years ago still
scores against current requirements without anyone rewriting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.contrib.postgres.search import TrigramSimilarity
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState

from ..models import (
    REVERSIBLE_PROVENANCE,
    Provenance,
    RelationKind,
    Skill,
    SkillAlias,
    SkillRelation,
    SkillStatus,
    SkillTranslation,
)
from ..normalisation import normalise

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

#: Above this, a trigram match is accepted. Tuned so "kubernets" resolves and
#: "java" never reaches JavaScript: that pair scores ~0.36.
TRIGRAM_THRESHOLD = 0.55

#: How far traversal will walk. A curation mistake can produce a cycle, and the
#: matcher must terminate whatever the data says.
MAX_DEPTH = 4

#: Re-exported: callers resolve and normalise together, and `normalise` lives in
#: its own module only because `SkillAlias.save()` needs it too.
__all__ = [
    "MAX_DEPTH",
    "TRIGRAM_THRESHOLD",
    "Match",
    "add_alias",
    "add_relation",
    "ancestors",
    "deprecate",
    "descendants",
    "implications",
    "merge",
    "normalise",
    "reinstate",
    "related",
    "revert_batch",
    "resolve",
    "resolve_many",
    "set_translation",
]


@dataclass(frozen=True)
class Match:
    skill: Skill
    confidence: Decimal
    method: str
    was_retired: bool = False


def resolve(text: str, *, locale: str = "en") -> Match | None:
    """Resolve free text to the skill in service today, or None.

    One text through the same batched path as a list, so the two can never
    answer the same question differently.
    """
    return resolve_many([text], locale=locale)[text]


def _forward(skill: Skill, confidence: Decimal, method: str) -> Match:
    """Follow a retired skill to its replacement, flagging that it happened.

    `was_retired` is surfaced rather than hidden, so an interface can say "we
    recorded this as Angular, which replaced AngularJS" instead of silently
    changing what someone wrote.
    """
    current = skill.resolve_current()
    return Match(current, confidence, method, was_retired=current.pk != skill.pk)


def resolve_many(texts: list[str], *, locale: str = "en") -> dict[str, Match | None]:
    """Resolve a whole list in a fixed number of queries.

    The layered order of `resolve` is unchanged — alias, canonical name,
    translation, trigram — and so is every answer it gives. What changes is
    that each layer is asked once for the whole list instead of once per term.

    It had to change. `reconcile` resolves every skill on a CV to report which
    of them the profile is missing, and a twelve-skill CV therefore ran
    twenty-four queries inside one page render: the CV builder screens issued
    forty-two against NFR-2's limit of twenty-five, and the count grew with the
    candidate's skills rather than with anything the page was doing.

    The trigram pass is a single query too, scoring every unresolved key at
    once, because it is reached exactly when a term is *not* in the taxonomy —
    which is the case where a list has many of them.
    """
    keys = {text: normalise(text) for text in texts}
    results: dict[str, Match | None] = dict.fromkeys(texts)

    pending = {key for key in keys.values() if key}
    if not pending:
        return results

    # The caller's locale first, then English: a Nepali-speaking candidate still
    # types "JS", and an English alias table is the one that is actually full.
    locales = [locale] if locale == "en" else [locale, "en"]
    by_key: dict[str, Match] = {}

    aliases = SkillAlias.objects.select_related("skill").filter(
        normalised__in=pending, locale__in=locales
    )
    # `(normalised, locale)` is unique, so the only choice left is which locale
    # wins — and that is the preference order above, not the row order.
    best_alias: dict[str, SkillAlias] = {}
    rank = {name: position for position, name in enumerate(locales)}
    for alias in aliases:
        held = best_alias.get(alias.normalised)
        if held is None or rank[alias.locale] < rank[held.locale]:
            best_alias[alias.normalised] = alias
    for key, alias in best_alias.items():
        by_key[key] = _forward(alias.skill, Decimal("1.00"), "alias")

    pending -= set(by_key)
    if pending:
        # `name__iexact=key` against an already case-folded key is `lower(name)
        # = key`, so the batch form matches exactly what one lookup matched.
        exact = Skill.objects.annotate(folded=Lower("name")).filter(folded__in=pending)
        for skill in exact:
            # `Skill.Meta.ordering` is by name, so the first row for a key is
            # the one a single `.first()` would have returned.
            by_key.setdefault(skill.folded, _forward(skill, Decimal("1.00"), "name"))

    pending -= set(by_key)
    if pending and locale != "en":
        translations = (
            SkillTranslation.objects.select_related("skill")
            .annotate(folded=Lower("name"))
            .filter(locale=locale, folded__in=pending)
        )
        for translation in translations:
            by_key.setdefault(
                translation.folded, _forward(translation.skill, Decimal("1.00"), "translation")
            )

    pending -= set(by_key)
    if pending:
        by_key.update(_trigram_matches(pending))

    for text, key in keys.items():
        results[text] = by_key.get(key)
    return results


def _trigram_matches(keys: set[str]) -> dict[str, Match]:
    """The fuzzy pass for every unresolved key, in one scan.

    One similarity column per key and one row per skill that clears the
    threshold for any of them, so the pick — highest similarity, and the
    taxonomy's own name order to break a tie, exactly as `.first()` did after
    `order_by("-similarity")` — happens over rows already in hand.
    """
    ordered = sorted(keys)
    annotations = {
        f"sim_{position}": TrigramSimilarity("name", key) for position, key in enumerate(ordered)
    }
    above = Q()
    for column in annotations:
        above |= Q(**{f"{column}__gte": TRIGRAM_THRESHOLD})

    matches: dict[str, Match] = {}
    scores: dict[str, float] = {}
    for skill in Skill.objects.active().annotate(**annotations).filter(above):
        for position, key in enumerate(ordered):
            similarity = getattr(skill, f"sim_{position}")
            if similarity is None or similarity < TRIGRAM_THRESHOLD:
                continue
            if key in scores and scores[key] >= similarity:
                continue
            scores[key] = similarity
            matches[key] = Match(skill, Decimal(str(round(similarity, 2))), "trigram")
    return matches


# --- traversal ------------------------------------------------------------


def implications(skill: Skill, *, max_depth: int = MAX_DEPTH) -> dict[int, Decimal]:
    """Skills this one partially satisfies, mapped to their credit.

    Django implies Python at 0.85; credit multiplies as it propagates. Traversal
    is breadth-first, bounded, and never revisits a skill — `implies` cycles are
    a plausible curation error and must not hang the matcher.
    """
    return _walk(skill, RelationKind.IMPLIES, max_depth=max_depth, multiply=True)


def ancestors(skill: Skill, *, max_depth: int = MAX_DEPTH) -> dict[int, Decimal]:
    """Broader skills, up the hierarchy."""
    return _walk(skill, RelationKind.BROADER, max_depth=max_depth, multiply=False)


def descendants(skill: Skill, *, max_depth: int = MAX_DEPTH) -> dict[int, Decimal]:
    """Narrower skills, down the hierarchy."""
    return _walk(skill, RelationKind.NARROWER, max_depth=max_depth, multiply=False)


def related(skill: Skill) -> list[Skill]:
    """Co-occurring skills, for suggestion only.

    Never scored: knowing React says nothing about knowing TypeScript, and
    counting co-occurrence as evidence inflates every match.
    """
    edges = skill.relations_out.filter(kind=RelationKind.RELATED).select_related("to_skill")
    return [edge.to_skill for edge in edges]


def _walk(skill: Skill, kind: str, *, max_depth: int, multiply: bool) -> dict[int, Decimal]:
    reached: dict[int, Decimal] = {}
    frontier: list[tuple[Skill, Decimal, int]] = [(skill, Decimal("1.00"), 0)]
    seen = {skill.pk}

    while frontier:
        current, carried, depth = frontier.pop(0)
        if depth >= max_depth:
            continue

        edges = current.relations_out.filter(kind=kind).select_related("to_skill")
        for edge in edges:
            value = carried * edge.weight if multiply else Decimal("1.00")
            target = edge.to_skill_id
            if value > reached.get(target, Decimal("0")):
                reached[target] = value
            if target not in seen:
                seen.add(target)
                frontier.append((edge.to_skill, value, depth + 1))

    return reached


# --- curation -------------------------------------------------------------


@transaction.atomic
def add_alias(
    *,
    skill: Skill,
    alias: str,
    locale: str = "en",
    actor: User | None = None,
    source: str = Provenance.CURATED,
    confidence: Decimal | None = None,
    reason: str = "",
    batch: str = "",
) -> SkillAlias:
    """Attach a surface form, refusing one that already means something else.

    Provenance is recorded on every alias (ADR-A47) because aliases will soon
    be written by extraction as well as by curators, and an alias a model
    guessed must be distinguishable from one a person decided on.
    """
    key = normalise(alias)
    if not key:
        raise InvalidState("An alias needs some text.")

    existing = (
        SkillAlias.objects.select_related("skill").filter(normalised=key, locale=locale).first()
    )
    if existing is not None:
        if existing.skill_id == skill.pk:
            return existing
        raise Conflict(
            f"{alias!r} already resolves to {existing.skill.name}. "
            "Merge the two skills rather than pointing one alias at both."
        )

    record = SkillAlias.objects.create(
        skill=skill,
        alias=alias.strip(),
        locale=locale,
        source=source,
        confidence=confidence,
        created_by=actor,
        created_reason=reason[:200],
        batch=batch,
    )
    audit.record(
        verb="skill.alias_added",
        actor=actor,
        target=skill,
        metadata={"alias": alias, "source": source, "batch": batch},
    )
    return record


@transaction.atomic
def add_relation(
    *,
    from_skill: Skill,
    to_skill: Skill,
    kind: str,
    weight: Decimal | None = None,
    actor: User | None = None,
    source: str = Provenance.CURATED,
    confidence: Decimal | None = None,
    reason: str = "",
    batch: str = "",
) -> SkillRelation:
    """Assert an edge between two skills, recording who says so.

    The only writer of relations outside the seed and the importer. A machine
    that writes edges directly bypasses the provenance that makes its work
    reversible, which is the whole point of ADR-A47.
    """
    if from_skill.pk == to_skill.pk:
        raise InvalidState("A skill cannot relate to itself.")
    if kind not in RelationKind.values:
        raise InvalidState(f"{kind!r} is not a kind of relation.")

    relation, created = SkillRelation.objects.get_or_create(
        from_skill=from_skill,
        to_skill=to_skill,
        kind=kind,
        defaults={
            "weight": weight if weight is not None else Decimal("1"),
            "source": source,
            "confidence": confidence,
            "created_by": actor,
            "created_reason": reason[:200],
            "batch": batch,
        },
    )
    if created:
        audit.record(
            verb="skill.relation_added",
            actor=actor,
            target=from_skill,
            metadata={"to": to_skill.slug, "kind": kind, "source": source, "batch": batch},
        )
    return relation


@transaction.atomic
def revert_batch(*, batch: str, actor: User | None = None) -> dict[str, int]:
    """Undo one machine-written run of aliases and relations.

    Only `inferred` and `ai_generated` rows are removed. A batch id that also
    covers curated or imported rows leaves those alone — an undo that deletes
    someone's deliberate work is worse than no undo, and the caller finds out
    from the counts rather than from a silent gap in the taxonomy.
    """
    if not batch.strip():
        raise InvalidState("Name the batch to revert.")

    aliases = SkillAlias.objects.filter(batch=batch, source__in=REVERSIBLE_PROVENANCE)
    relations = SkillRelation.objects.filter(batch=batch, source__in=REVERSIBLE_PROVENANCE)

    counts = {"aliases": aliases.count(), "relations": relations.count()}
    protected = (
        SkillAlias.objects.filter(batch=batch).exclude(source__in=REVERSIBLE_PROVENANCE).count()
        + SkillRelation.objects.filter(batch=batch)
        .exclude(source__in=REVERSIBLE_PROVENANCE)
        .count()
    )
    counts["kept"] = protected

    aliases.delete()
    relations.delete()

    audit.record(verb="taxonomy.batch_reverted", actor=actor, metadata={"batch": batch, **counts})
    return counts


@transaction.atomic
def set_translation(
    *, skill: Skill, locale: str, name: str, actor: User | None = None
) -> SkillTranslation:
    """Set the display name for a locale."""
    if locale == "en":
        raise InvalidState("English is Skill.name; edit that rather than adding a translation.")
    if not name.strip():
        raise InvalidState("A translation needs a name.")

    translation, _ = SkillTranslation.objects.update_or_create(
        skill=skill, locale=locale, defaults={"name": name.strip()}
    )
    audit.record(verb="skill.translated", actor=actor, target=skill, metadata={"locale": locale})
    return translation


@transaction.atomic
def deprecate(
    *,
    skill: Skill,
    reason: str,
    replacement: Skill | None = None,
    actor: User | None = None,
) -> Skill:
    """Retire a skill that was genuinely distinct and is now obsolete.

    Different from a merge: AngularJS was never Angular, so the historical fact
    that someone knew AngularJS stays true and stays readable. A replacement is
    optional — sometimes a technology simply stops being asked for.
    """
    if skill.is_retired:
        raise InvalidState(f"{skill.name} is already {skill.get_status_display().lower()}.")
    if not reason.strip():
        raise InvalidState("Record why this skill is being deprecated.")
    if replacement is not None:
        _guard_replacement(skill, replacement)

    skill.status = SkillStatus.DEPRECATED
    skill.replaced_by = replacement
    skill.retired_reason = reason.strip()
    skill.retired_at = timezone.now()
    skill.save(
        update_fields=["status", "replaced_by", "retired_reason", "retired_at", "updated_at"]
    )

    audit.record(
        verb="skill.deprecated",
        actor=actor,
        target=skill,
        metadata={"reason": reason, "replacement": replacement.slug if replacement else None},
    )
    return skill


@transaction.atomic
def reinstate(*, skill: Skill, actor: User | None = None) -> Skill:
    """Bring a deprecated skill back into service.

    Merged skills cannot be reinstated: a merge asserts the two rows always
    meant the same thing, and un-asserting it would split data that has since
    been written against the survivor.
    """
    if skill.status != SkillStatus.DEPRECATED:
        raise InvalidState("Only a deprecated skill can be reinstated.")

    skill.status = SkillStatus.ACTIVE
    skill.replaced_by = None
    skill.retired_reason = ""
    skill.retired_at = None
    skill.save(
        update_fields=["status", "replaced_by", "retired_reason", "retired_at", "updated_at"]
    )
    audit.record(verb="skill.reinstated", actor=actor, target=skill)
    return skill


@transaction.atomic
def merge(*, source: Skill, target: Skill, actor: User | None = None) -> Skill:
    """Fold a duplicate into the skill it always was.

    Everything pointing at the source moves. The source row survives with
    `status=merged` — deleting it would orphan aliases, relations and every
    CandidateSkill that referenced it, and make the mistake unrecoverable.
    """
    _guard_replacement(source, target)

    # Aliases and external ids are globally unique, so they can only ever be
    # held by one of the two skills: repointing them cannot collide.
    source.aliases.update(skill=target)
    source.external_ids.update(skill=target)

    # The source's own name becomes an alias of the target, or the merge loses
    # the very spelling that made someone create the duplicate.
    key = normalise(source.name)
    if not SkillAlias.objects.filter(normalised=key, locale="en").exists():
        SkillAlias.objects.create(
            skill=target,
            alias=source.name,
            locale="en",
            source=Provenance.CURATED,
            created_by=actor,
            created_reason=f"Kept when {source.slug} was merged into {target.slug}.",
        )

    # Translations are unique per (skill, locale), so both skills can carry the
    # same locale. The target's wins: it is the row curators have been editing.
    for translation in source.translations.all():
        if target.translations.filter(locale=translation.locale).exists():
            translation.delete()
        else:
            translation.skill = target
            translation.save(update_fields=["skill", "updated_at"])

    for relation in list(source.relations_out.all()):
        if relation.to_skill_id == target.pk:
            relation.delete()
            continue
        relation.from_skill = target
        _save_or_drop(relation)

    for relation in list(source.relations_in.all()):
        if relation.from_skill_id == target.pk:
            relation.delete()
            continue
        relation.to_skill = target
        _save_or_drop(relation)

    source.status = SkillStatus.MERGED
    source.replaced_by = target
    source.retired_at = timezone.now()
    source.retired_reason = f"Merged into {target.name}."
    source.save(
        update_fields=["status", "replaced_by", "retired_at", "retired_reason", "updated_at"]
    )

    audit.record(
        verb="skill.merged",
        actor=actor,
        target=target,
        metadata={"source": source.slug, "target": target.slug},
    )
    return target


def _guard_replacement(source: Skill, target: Skill) -> None:
    """Refuse a replacement that is self-referential or would loop."""
    if source.pk == target.pk:
        raise InvalidState("A skill cannot replace itself.")
    if target.resolve_current().pk == source.pk:
        raise InvalidState("That would create a replacement cycle.")


def _save_or_drop(relation: SkillRelation) -> None:
    """Repoint an edge, discarding it if the target already has that edge."""
    try:
        with transaction.atomic():
            relation.save()
    except IntegrityError:
        relation.delete()
