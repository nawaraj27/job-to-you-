"""
The part that writes.

Three properties matter more than speed, because an import touches the table
every match in the system reads:

  * **Idempotent.** Running the same export twice produces the same rows. Skills
    are keyed on (source, external_id), not on a name, so an upstream rename
    updates a skill rather than forking it.
  * **Resumable.** Work commits in batches and each batch checkpoints the last
    id it wrote. A run that dies at row 22,000 of 30,000 resumes there instead
    of starting again — and, because the writes are idempotent, resuming a few
    rows early is harmless.
  * **Non-destructive.** An import may fill gaps; it may not overwrite curation.
    It never renames a skill, never steals an alias another skill already
    claims, and never replaces a description or category a curator has set.
    A source that could quietly rewrite the taxonomy is a source that can break
    matching platform-wide with one bad export.

Hierarchy is linked in a second pass over the same source, because a parent can
appear after its child under any ordering keyed on id. The pass has its own
checkpoint, so resumption knows which half of the work it is in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.audit import services as audit

from ..models import (
    ImportRun,
    Provenance,
    RelationKind,
    Skill,
    SkillAlias,
    SkillCategory,
    SkillExternalId,
    SkillKind,
    SkillRelation,
    SkillTranslation,
)
from ..services.skills import normalise
from .base import SkillRecord, TaxonomySource

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

DEFAULT_BATCH_SIZE = 200

Progress = Callable[[str], None]


@dataclass
class ImportSummary:
    run_id: int | None
    source: str
    processed: int
    created: int
    updated: int
    skipped: int
    linked: int
    resumed_from: str = ""
    dry_run: bool = False

    def __str__(self) -> str:
        head = "would import" if self.dry_run else "imported"
        return (
            f"{head} {self.source}: {self.processed} processed, {self.created} created, "
            f"{self.updated} updated, {self.skipped} unchanged, {self.linked} relations"
        )


class _DryRun(Exception):
    """Unwinds the transaction a dry run did its counting in."""


def run_import(
    source: TaxonomySource,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    resume: bool = True,
    dry_run: bool = False,
    actor: User | None = None,
    on_progress: Progress | None = None,
) -> ImportSummary:
    """Import every record a source offers, recording progress as it goes."""
    if dry_run:
        return _dry_run(source, batch_size=batch_size)

    run, resumed_from = _run_for(source, resume=resume)
    report = on_progress or (lambda _message: None)

    try:
        if run.phase == ImportRun.Phase.RECORDS:
            _import_records(source, run, batch_size=batch_size, report=report)
            _advance_to_relations(run)

        _link_relations(source, run, batch_size=batch_size, report=report)

        run.state = ImportRun.State.COMPLETED
        run.finished_at = timezone.now()
        run.error = ""
        run.save(update_fields=["state", "finished_at", "error", "updated_at"])

    except Exception as exc:
        # The checkpoint survives: batches already committed stay committed, and
        # `last_key` names where to pick up. Re-raised so the caller still fails.
        run.state = ImportRun.State.FAILED
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        run.finished_at = timezone.now()
        run.save(update_fields=["state", "error", "finished_at", "updated_at"])
        raise

    audit.record(
        verb="taxonomy.imported",
        actor=actor,
        target=run,
        metadata={
            "source": run.source,
            "created": run.created,
            "updated": run.updated,
            "linked": run.linked,
        },
    )
    return _summarise(run, resumed_from=resumed_from)


# --- run bookkeeping ------------------------------------------------------


def _run_for(source: TaxonomySource, *, resume: bool) -> tuple[ImportRun, str]:
    """Continue the last unfinished run for this source, or start one."""
    if resume:
        unfinished = (
            ImportRun.objects.filter(source=source.source)
            .exclude(state=ImportRun.State.COMPLETED)
            .order_by("-created_at")
            .first()
        )
        if unfinished is not None:
            resumed_from = unfinished.last_key
            unfinished.state = ImportRun.State.RUNNING
            unfinished.error = ""
            unfinished.save(update_fields=["state", "error", "updated_at"])
            return unfinished, resumed_from

    return ImportRun.objects.create(source=source.source), ""


def _advance_to_relations(run: ImportRun) -> None:
    run.phase = ImportRun.Phase.RELATIONS
    run.last_key = ""
    run.save(update_fields=["phase", "last_key", "updated_at"])


def _summarise(run: ImportRun, *, resumed_from: str = "", dry_run: bool = False) -> ImportSummary:
    return ImportSummary(
        # A dry run's row is rolled back, so reporting its id would point at
        # something that does not exist.
        run_id=None if dry_run else run.pk,
        source=run.source,
        processed=run.processed,
        created=run.created,
        updated=run.updated,
        skipped=run.skipped,
        linked=run.linked,
        resumed_from=resumed_from,
        dry_run=dry_run,
    )


def _batched(records: Iterator[SkillRecord], size: int) -> Iterator[list[SkillRecord]]:
    batch: list[SkillRecord] = []
    for record in records:
        batch.append(record)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# --- phase one: skills ----------------------------------------------------


def _import_records(
    source: TaxonomySource, run: ImportRun, *, batch_size: int, report: Progress
) -> None:
    highest = run.last_key

    for batch in _batched(source.records(after=run.last_key), batch_size):
        with transaction.atomic():
            for record in batch:
                if record.external_id <= highest:
                    # A source that ignores `after`, or one whose order slipped.
                    # Cheaper to skip than to trust it.
                    continue

                outcome = _upsert(record, source=source.source)
                run.processed += 1
                setattr(run, outcome, getattr(run, outcome) + 1)
                highest = max(highest, record.external_id)

            run.last_key = highest
            run.save(
                update_fields=[
                    "processed",
                    "created",
                    "updated",
                    "skipped",
                    "last_key",
                    "updated_at",
                ]
            )

        report(f"{run.source}: {run.processed} records")


def _upsert(record: SkillRecord, *, source: str) -> str:
    """Create or enrich one skill. Returns 'created', 'updated' or 'skipped'."""
    skill, created = _find_or_create(record, source=source)
    changed = _enrich(skill, record)
    changed |= _apply_aliases(skill, record, source)
    changed |= _apply_translations(skill, record)

    if created:
        return "created"
    return "updated" if changed else "skipped"


def _find_or_create(record: SkillRecord, *, source: str) -> tuple[Skill, bool]:
    """Locate the skill this record refers to, creating it only if truly new.

    Match order: this source's own id, then an exact alias or name. Exact only —
    a fuzzy match here would fold "Java" into "JavaScript" across ten thousand
    rows in one command, with no human in the loop.
    """
    external = (
        SkillExternalId.objects.select_related("skill")
        .filter(source=source, external_id=record.external_id)
        .first()
    )
    if external is not None:
        # Forward through a merge: the id belongs to whatever the skill became.
        return external.skill.resolve_current(), False

    key = normalise(record.name)
    alias = SkillAlias.objects.select_related("skill").filter(normalised=key, locale="en").first()
    match = alias.skill if alias else Skill.objects.filter(name__iexact=record.name.strip()).first()

    created = False
    if match is None:
        match = Skill.objects.create(
            slug=_unique_slug(record.name),
            name=record.name.strip(),
            description=record.description,
        )
        created = True
    else:
        match = match.resolve_current()

    SkillExternalId.objects.get_or_create(
        source=source,
        external_id=record.external_id,
        defaults={"skill": match, "uri": record.uri},
    )
    return match, created


def _enrich(skill: Skill, record: SkillRecord) -> bool:
    """Fill blanks only. A curator's description outranks any export."""
    fields: list[str] = []

    if record.kind and record.kind in SkillKind.values and skill.kind == SkillKind.UNKNOWN:
        skill.kind = record.kind
        fields.append("kind")

    if record.description and not skill.description:
        skill.description = record.description
        fields.append("description")

    if record.category and skill.category_id is None:
        category, _ = SkillCategory.objects.get_or_create(
            slug=slugify(record.category)[:80] or "uncategorised",
            defaults={"name": record.category},
        )
        skill.category = category
        fields.append("category")

    if fields:
        skill.save(update_fields=[*fields, "updated_at"])
    return bool(fields)


def _apply_aliases(skill: Skill, record: SkillRecord, source_name: str) -> bool:
    added = False
    for alias in record.aliases:
        key = normalise(alias)
        if not key:
            continue
        claimed = SkillAlias.objects.filter(normalised=key, locale="en").first()
        if claimed is not None:
            # Already means something. An import never repoints an alias — that
            # would change what existing profiles say without anyone deciding to.
            continue
        SkillAlias.objects.create(
            skill=skill,
            alias=alias,
            locale="en",
            source=Provenance.IMPORTED,
            created_reason=f"Imported with {record.external_id}.",
            batch=_batch_for(source_name),
        )
        added = True
    return added


def _apply_translations(skill: Skill, record: SkillRecord) -> bool:
    added = False
    for locale, name in record.translations.items():
        if locale == "en" or not name.strip():
            continue
        _, was_created = SkillTranslation.objects.get_or_create(
            skill=skill, locale=locale, defaults={"name": name.strip()}
        )
        added |= was_created
    return added


def _unique_slug(name: str) -> str:
    base = slugify(name)[:100] or "skill"
    slug, suffix = base, 2
    while Skill.objects.filter(slug=slug).exists():
        tail = f"-{suffix}"
        slug = f"{base[: 100 - len(tail)]}{tail}"
        suffix += 1
    return slug


# --- phase two: hierarchy -------------------------------------------------


def _link_relations(
    source: TaxonomySource, run: ImportRun, *, batch_size: int, report: Progress
) -> None:
    """Second pass: connect each skill to its broader skills.

    Separate from phase one because a parent may appear after its child. Every
    target exists by now, so an unresolved id means the export references a
    concept it did not ship — recorded as skipped, never invented.
    """
    highest = run.last_key

    for batch in _batched(source.records(after=run.last_key), batch_size):
        with transaction.atomic():
            for record in batch:
                if record.external_id <= highest:
                    continue
                run.linked += _link_one(record, source=source.source)
                highest = max(highest, record.external_id)

            run.last_key = highest
            run.save(update_fields=["linked", "last_key", "updated_at"])

        report(f"{run.source}: {run.linked} relations")


def _link_one(record: SkillRecord, *, source: str) -> int:
    if not record.broader:
        return 0

    child = _skill_for(record.external_id, source=source)
    if child is None:
        return 0

    linked = 0
    for parent_id in record.broader:
        parent = _skill_for(parent_id, source=source)
        if parent is None or parent.pk == child.pk:
            continue

        # Both directions, so `ancestors()` and `descendants()` each answer
        # from one indexed edge rather than a reverse scan.
        provenance = {
            "source": Provenance.IMPORTED,
            "created_reason": f"Hierarchy from {source}.",
            "batch": _batch_for(source),
        }
        _, up = SkillRelation.objects.get_or_create(
            from_skill=child, to_skill=parent, kind=RelationKind.BROADER, defaults=provenance
        )
        _, down = SkillRelation.objects.get_or_create(
            from_skill=parent, to_skill=child, kind=RelationKind.NARROWER, defaults=provenance
        )
        linked += int(up) + int(down)
    return linked


def _batch_for(source: str) -> str:
    """One batch id per source, so an import's rows are identifiable (ADR-A47).

    Imported rows are never removed by `revert_batch` — they are somebody's
    deliberate data, not a guess — but they still carry the id so an operator
    can see what a run touched.
    """
    return f"import:{source}"


def _skill_for(external_id: str, *, source: str) -> Skill | None:
    external = (
        SkillExternalId.objects.select_related("skill")
        .filter(source=source, external_id=external_id)
        .first()
    )
    return external.skill.resolve_current() if external else None


# --- dry run --------------------------------------------------------------


def _dry_run(source: TaxonomySource, *, batch_size: int) -> ImportSummary:
    """Report what an import would do, then unwind every write.

    Counted by doing the real work inside a transaction that is rolled back,
    rather than by a parallel "what would happen" implementation — the second
    of which is always subtly wrong about the case you care about.
    """
    run = ImportRun(source=source.source)
    try:
        with transaction.atomic():
            run.save()
            _import_records(source, run, batch_size=batch_size, report=lambda _m: None)
            _advance_to_relations(run)
            _link_relations(source, run, batch_size=batch_size, report=lambda _m: None)
            raise _DryRun
    except _DryRun:
        pass

    return _summarise(run, dry_run=True)
