"""
The draft: autosave, editing, and the bounded undo ring (ADR-A19).

**Autosave creates no versions.** That is the single rule this module exists to
keep, and it is why every write here ends at `ResumeDraft` and never at
`ResumeVersion`.

Every mutation goes through `_edit`, which does four things in one place:
checks the optimistic lock, snapshots the state *before* the change, applies
it, and bumps the version. Doing it per-operation would mean twelve chances to
forget the snapshot, and a missing snapshot is an undo that silently skips a
step.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.common.errors import InvalidState, NotFound, StaleWrite

from .. import content as content_rules
from ..models import ResumeDraft, ResumeDraftSnapshot

if TYPE_CHECKING:  # pragma: no cover
    pass

#: How many undo steps survive. Deep enough to recover from a mistaken bulk
#: action, shallow enough that a long session does not grow without bound.
SNAPSHOT_LIMIT = 25

MAX_BULLETS = 12
MAX_ITEMS = 40
MAX_SECTIONS = 15
MAX_ENTRIES = 25

Mutator = Callable[[dict], Any]


# --- the one write path ---------------------------------------------------


def _edit(
    draft: ResumeDraft,
    mutate: Mutator,
    *,
    label: str,
    expected_version: int | None = None,
) -> Any:
    """Snapshot, mutate, save. The only way draft content changes."""
    if expected_version is not None and expected_version != draft.row_version:
        raise StaleWrite(
            "This CV was changed in another tab. Reload before saving again; "
            "your other window has a newer version."
        )

    with transaction.atomic():
        _snapshot(draft, label=label)

        working = copy.deepcopy(draft.content)
        result = mutate(working)

        draft.content = working
        draft.row_version += 1
        draft.dirty = True
        draft.saved_at = timezone.now()
        draft.save(update_fields=["content", "row_version", "dirty", "saved_at", "updated_at"])

    return result


def _snapshot(draft: ResumeDraft, *, label: str) -> None:
    """Keep the state before this change, and trim the ring.

    Trimmed by deleting the oldest rows rather than by rewriting sequence
    numbers: renumbering twenty-five rows on every keystroke is the kind of
    write amplification that only shows up under load.
    """
    highest = draft.snapshots.aggregate(top=Max("seq"))["top"] or 0
    ResumeDraftSnapshot.objects.create(
        draft=draft, seq=highest + 1, content=copy.deepcopy(draft.content), label=label[:80]
    )

    keep = list(draft.snapshots.order_by("-seq").values_list("pk", flat=True)[:SNAPSHOT_LIMIT])
    draft.snapshots.exclude(pk__in=keep).delete()


# --- whole-document writes ------------------------------------------------


def autosave(
    *, draft: ResumeDraft, content: dict, expected_version: int | None = None
) -> ResumeDraft:
    """What the builder calls as the candidate types.

    Writes the draft and nothing else — no version, no event, no audit row.
    Autosave firing every few seconds must be the cheapest write in the system.
    """
    if not isinstance(content, dict) or "sections" not in content:
        raise InvalidState("That is not a resume document.")

    _edit(
        draft,
        lambda working: working.update(content),
        label="autosave",
        expected_version=expected_version,
    )
    return draft


def replace(*, draft: ResumeDraft, content: dict, label: str) -> ResumeDraft:
    """Put a whole document in the draft's place, keeping nothing.

    Distinct from `autosave`, which merges: autosave carries a document the
    builder just edited, so merging a partial payload is harmless. Restoring an
    old version is the opposite promise — anything the draft has picked up
    since must go, or "restore" hands back a hybrid of two documents that never
    existed.
    """
    if not isinstance(content, dict) or "sections" not in content:
        raise InvalidState("That is not a resume document.")

    def mutate(working: dict) -> None:
        working.clear()
        working.update(copy.deepcopy(content))

    _edit(draft, mutate, label=label)
    return draft


def set_contact(*, draft: ResumeDraft, field: str, value: str) -> ResumeDraft:
    allowed = {"name", "headline", "email", "phone", "location"}
    if field not in allowed:
        raise InvalidState("That is not a contact field.")

    def mutate(working: dict) -> None:
        working.setdefault("contact", {})[field] = (value or "").strip()[:200]

    _edit(draft, mutate, label=f"contact:{field}")
    return draft


# --- sections -------------------------------------------------------------


def add_section(*, draft: ResumeDraft, title: str, kind: str = "entries") -> str:
    if kind not in content_rules.SECTION_KINDS:
        raise InvalidState("That is not a kind of section.")
    if not (title or "").strip():
        raise InvalidState("Give the section a title.")

    def mutate(working: dict) -> str:
        sections = working.setdefault("sections", [])
        if len(sections) >= MAX_SECTIONS:
            raise InvalidState(f"A CV holds up to {MAX_SECTIONS} sections.")

        section_id = content_rules.new_id()
        section: dict[str, Any] = {
            "id": section_id,
            "key": "",
            "title": title.strip()[:60],
            "kind": kind,
            "visible": True,
            "order": len(sections),
        }
        if kind == "text":
            section["text"] = ""
        elif kind == "entries":
            section["entries"] = []
        else:
            section["items"] = []

        sections.append(section)
        return section_id

    return str(_edit(draft, mutate, label="add section"))


def rename_section(*, draft: ResumeDraft, section_id: str, title: str) -> ResumeDraft:
    if not (title or "").strip():
        raise InvalidState("Give the section a title.")

    def mutate(working: dict) -> None:
        _require_section(working, section_id)["title"] = title.strip()[:60]

    _edit(draft, mutate, label="rename section")
    return draft


def toggle_section(*, draft: ResumeDraft, section_id: str) -> bool:
    def mutate(working: dict) -> bool:
        section = _require_section(working, section_id)
        section["visible"] = not section.get("visible", True)
        return section["visible"]

    return bool(_edit(draft, mutate, label="toggle section"))


def move_section(*, draft: ResumeDraft, section_id: str, offset: int) -> ResumeDraft:
    def mutate(working: dict) -> None:
        sections = working.get("sections", [])
        index = next((i for i, s in enumerate(sections) if s.get("id") == section_id), None)
        if index is None:
            raise NotFound("That section is not on this CV.")

        target = max(0, min(len(sections) - 1, index + offset))
        sections.insert(target, sections.pop(index))
        for position, section in enumerate(sections):
            section["order"] = position

    _edit(draft, mutate, label="move section")
    return draft


def reorder_sections(*, draft: ResumeDraft, order: list[str]) -> ResumeDraft:
    """Put the sections in the order given, in one move.

    `move_section` shifts one section by one place, which is what a pair of
    arrow buttons needs. A drag lands a section several places away in a single
    gesture, and expressing that as a run of offsets means a request per step
    and a preview that flickers through positions the candidate never chose.

    Ids the CV does not have are refused rather than ignored: a reorder built
    from a stale page would otherwise silently drop whatever was added since,
    and a section disappearing from a CV is not an acceptable way to learn that
    two tabs were open.
    """

    def mutate(working: dict) -> None:
        sections = working.get("sections", [])
        by_id = {section.get("id"): section for section in sections}

        if set(order) != set(by_id):
            raise InvalidState(
                "This CV changed in another tab. Reload the builder and try that move again."
            )

        working["sections"] = [by_id[section_id] for section_id in order]
        for position, section in enumerate(working["sections"]):
            section["order"] = position

    _edit(draft, mutate, label="reorder sections")
    return draft


def delete_section(*, draft: ResumeDraft, section_id: str) -> ResumeDraft:
    def mutate(working: dict) -> None:
        sections = working.get("sections", [])
        section = _require_section(working, section_id)
        sections.remove(section)
        for position, remaining in enumerate(sections):
            remaining["order"] = position

    _edit(draft, mutate, label="delete section")
    return draft


def set_section_text(*, draft: ResumeDraft, section_id: str, text: str) -> ResumeDraft:
    def mutate(working: dict) -> None:
        section = _require_section(working, section_id)
        if section.get("kind") != "text":
            raise InvalidState("That section does not hold prose.")
        section["text"] = (text or "").strip()[:4000]
        if section.get("key") == "summary":
            # The summary appears twice in the rendered document; keeping them
            # in step here means no template has to know that.
            working["summary"] = section["text"]

    _edit(draft, mutate, label="edit summary")
    return draft


def set_section_items(*, draft: ResumeDraft, section_id: str, items: list[str]) -> ResumeDraft:
    def mutate(working: dict) -> None:
        section = _require_section(working, section_id)
        if section.get("kind") != "list":
            raise InvalidState("That section does not hold a list.")
        cleaned = [str(item).strip()[:80] for item in items if str(item).strip()]
        if len(cleaned) > MAX_ITEMS:
            raise InvalidState(f"That list holds up to {MAX_ITEMS} items.")
        section["items"] = cleaned

    _edit(draft, mutate, label="edit list")
    return draft


# --- entries and bullets --------------------------------------------------


def add_entry(*, draft: ResumeDraft, section_id: str, **fields: Any) -> str:
    def mutate(working: dict) -> str:
        section = _require_section(working, section_id)
        if section.get("kind") != "entries":
            raise InvalidState("That section does not hold entries.")

        entries = section.setdefault("entries", [])
        if len(entries) >= MAX_ENTRIES:
            raise InvalidState(f"That section holds up to {MAX_ENTRIES} entries.")

        entry = content_rules.new_entry(**fields)
        entries.append(entry)
        return entry["id"]

    return str(_edit(draft, mutate, label="add entry"))


def ensure_entry(*, draft: ResumeDraft, section_id: str, entry_id: str) -> str:
    """Make sure an entry with this id exists, creating an empty one if not.

    For a suggestion aimed at a role the candidate does not have yet. The
    assistant builder is the case: somebody with an empty CV describes their
    job, and the bullets need somewhere to go — but nothing may touch the
    document before they accept (INV-3), so the id is reserved when the
    suggestion is offered and the role appears on the first accept.

    Idempotent, because a turn proposes several bullets and each one accepted
    in turn must land in the same role rather than making another.
    """
    if content_rules.entry_by_id(draft.content, entry_id) is not None:
        return entry_id

    def mutate(working: dict) -> str:
        section = _require_section(working, section_id)
        if section.get("kind") != "entries":
            raise InvalidState("That section does not hold entries.")

        entries = section.setdefault("entries", [])
        if len(entries) >= MAX_ENTRIES:
            raise InvalidState(f"That section holds up to {MAX_ENTRIES} entries.")

        entry = content_rules.new_entry()
        entry["id"] = entry_id
        entries.append(entry)
        return entry_id

    return str(_edit(draft, mutate, label="add entry"))


def update_entry(*, draft: ResumeDraft, entry_id: str, **fields: Any) -> ResumeDraft:
    """Edit one entry, and mark it diverged if it came from the profile.

    The `linked → edited` transition is the whole reconcile protocol in one
    line: the moment a candidate rewords a profile-sourced entry, the profile
    stops owning that wording and drift becomes visible instead of silent
    (§5.5.3).
    """
    editable = {"title", "org", "dates"}

    def mutate(working: dict) -> None:
        found = content_rules.entry_by_id(working, entry_id)
        if found is None:
            raise NotFound("That entry is not on this CV.")

        _section, entry = found
        for key, value in fields.items():
            if key in editable:
                entry[key] = str(value or "").strip()[:200]

        if entry.get("sync_state") == content_rules.SyncState.LINKED:
            entry["sync_state"] = content_rules.SyncState.EDITED

    _edit(draft, mutate, label="edit entry")
    return draft


def move_entry(*, draft: ResumeDraft, entry_id: str, offset: int) -> ResumeDraft:
    """Move one entry up or down within its own section.

    A CV is read in reverse chronological order, and there was no way to put a
    role in one. An entry added after the fact — or imported in the order a PDF
    happened to extract — stayed wherever it landed, and the only remedy was to
    delete it and retype it underneath.

    Clamped rather than wrapped: pressing "up" on the first role should do
    nothing, not send it to the bottom.
    """

    def mutate(working: dict) -> None:
        found = content_rules.entry_by_id(working, entry_id)
        if found is None:
            raise NotFound("That entry is not on this CV.")

        section, entry = found
        entries = section["entries"]
        index = entries.index(entry)
        target = max(0, min(len(entries) - 1, index + offset))
        entries.insert(target, entries.pop(index))

    _edit(draft, mutate, label="move entry")
    return draft


def reorder_entries(*, draft: ResumeDraft, section_id: str, order: list[str]) -> ResumeDraft:
    """Put one section's entries in the order given, in a single move.

    What a drag produces. `move_entry` shifts by one, which is what the arrow
    buttons need; expressing a drag as a run of offsets would be a request per
    step and a preview that flickers through positions nobody chose.

    Ids the section does not hold are refused rather than ignored, for the same
    reason `reorder_sections` refuses them: an order built from a stale page
    would otherwise drop whatever was added since, and a role vanishing from a
    CV is not an acceptable way to find out that two tabs were open.
    """

    def mutate(working: dict) -> None:
        section = _require_section(working, section_id)
        if section.get("kind") != "entries":
            raise InvalidState("That section does not hold entries.")

        entries = section.get("entries", [])
        by_id = {entry.get("id"): entry for entry in entries}
        if set(order) != set(by_id):
            raise InvalidState(
                "This CV changed in another tab. Reload the builder and try that move again."
            )

        section["entries"] = [by_id[entry_id] for entry_id in order]

    _edit(draft, mutate, label="reorder entries")
    return draft


def duplicate_entry(*, draft: ResumeDraft, entry_id: str) -> str:
    """Copy one entry, directly below the original.

    Two roles at one employer, or the same role described for a second
    audience, is the most common thing somebody wants to do twice — and doing
    it by hand means retyping every bullet.

    The copy is its own entry: a new id, and never `linked`, because it did not
    come from the profile however the original got here (§5.5.3).
    """

    def mutate(working: dict) -> str:
        found = content_rules.entry_by_id(working, entry_id)
        if found is None:
            raise NotFound("That entry is not on this CV.")

        section, entry = found
        entries = section["entries"]
        if len(entries) >= MAX_ENTRIES:
            raise InvalidState(f"That section holds up to {MAX_ENTRIES} entries.")

        duplicate = copy.deepcopy(entry)
        duplicate["id"] = content_rules.new_id()
        duplicate["sync_state"] = content_rules.SyncState.DETACHED
        duplicate.pop("source_ref", None)
        entries.insert(entries.index(entry) + 1, duplicate)
        return duplicate["id"]

    return str(_edit(draft, mutate, label="duplicate entry"))


def delete_entry(*, draft: ResumeDraft, entry_id: str) -> ResumeDraft:
    def mutate(working: dict) -> None:
        found = content_rules.entry_by_id(working, entry_id)
        if found is None:
            raise NotFound("That entry is not on this CV.")
        section, entry = found
        section["entries"].remove(entry)

    _edit(draft, mutate, label="delete entry")
    return draft


def set_bullets(*, draft: ResumeDraft, entry_id: str, bullets: list[str]) -> ResumeDraft:
    def mutate(working: dict) -> None:
        found = content_rules.entry_by_id(working, entry_id)
        if found is None:
            raise NotFound("That entry is not on this CV.")

        _section, entry = found
        cleaned = [str(line).strip()[:300] for line in bullets if str(line).strip()]
        if len(cleaned) > MAX_BULLETS:
            raise InvalidState(f"An entry holds up to {MAX_BULLETS} bullets.")

        entry["bullets"] = cleaned
        if entry.get("sync_state") == content_rules.SyncState.LINKED:
            entry["sync_state"] = content_rules.SyncState.EDITED

    _edit(draft, mutate, label="edit bullets")
    return draft


# --- undo -----------------------------------------------------------------


def undo(*, draft: ResumeDraft) -> bool:
    """Step back one state. Returns False when there is nothing to undo.

    The snapshot being restored is consumed: undo walks backwards through the
    ring, and leaving it in place would make the next undo a no-op that looks
    like a bug.
    """
    latest = draft.snapshots.order_by("-seq").first()
    if latest is None:
        return False

    with transaction.atomic():
        draft.content = latest.content
        draft.row_version += 1
        draft.dirty = True
        draft.save(update_fields=["content", "row_version", "dirty", "updated_at"])
        latest.delete()

    return True


def history(*, draft: ResumeDraft, limit: int = SNAPSHOT_LIMIT) -> list[ResumeDraftSnapshot]:
    return list(draft.snapshots.order_by("-seq")[:limit])


# --- internals ------------------------------------------------------------


def _require_section(content: dict, section_id: str) -> dict:
    section = content_rules.section_by_id(content, section_id)
    if section is None:
        raise NotFound("That section is not on this CV.")
    return section
