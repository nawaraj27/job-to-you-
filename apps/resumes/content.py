"""
The shape a resume document holds, and the rules for touching it.

`ResumeDraft.content` and `ResumeVersion.content` hold exactly what
`templates/resume/_paper.html` renders, and the PDF and DOCX exporters flatten
through `rendering/layout.py` (SRS §5.5.2):

    {contact, summary, sections: [{key, title, kind, visible, order,
                                   entries: [{title, org, dates, bullets[],
                                              source_ref, sync_state}]}]}

Two things live here rather than in a service because both the draft service
and the version service need them and neither owns them:

  * **the canonical hash** — `sha256` of the content in a stable form, which is
    what lets "analyse" reuse an existing version instead of creating an
    identical one (SRS §5.5.1);
  * **`source_ref` / `sync_state`** — the per-entry provenance the reconcile
    protocol reads (§5.5.3). An entry that came from the profile carries the
    row it came from and whether it has since diverged.
"""

from __future__ import annotations

import uuid
from typing import Any

from apps.common.hashing import stable_hash

CONTENT_SCHEMA_VERSION = 1

#: Sections a new document starts with. Order is the order they render in, and
#: the hidden ones exist so turning a section on is a toggle rather than a
#: creation flow the candidate has to discover.
DEFAULT_SECTIONS: tuple[tuple[str, str, str, bool], ...] = (
    ("summary", "Summary", "text", True),
    ("experience", "Experience", "entries", True),
    ("skills", "Skills", "list", True),
    ("education", "Education", "entries", True),
    ("languages", "Languages", "list", False),
    ("projects", "Projects", "entries", False),
    ("certifications", "Certifications", "list", False),
)

SECTION_KINDS = frozenset({"text", "entries", "list"})


class SyncState:
    """How a document entry relates to the profile row it came from (§5.5.3)."""

    LINKED = "linked"  # follows the profile
    EDITED = "edited"  # diverged; reported as drift
    DETACHED = "detached"  # document-only by choice


def new_id() -> str:
    """Stable ids for sections and entries, so an edit can name its target."""
    return uuid.uuid4().hex[:12]


def blank_document(*, name: str = "", headline: str = "", email: str = "") -> dict[str, Any]:
    """An empty CV, ready to be written into."""
    return {
        "schema_version": CONTENT_SCHEMA_VERSION,
        "contact": {
            "name": name,
            "headline": headline,
            "email": email,
            "phone": "",
            "location": "",
        },
        "summary": "",
        "sections": [
            {
                "id": new_id(),
                "key": key,
                "title": title,
                "kind": kind,
                "visible": visible,
                "order": order,
                **({"text": ""} if kind == "text" else {}),
                **({"entries": []} if kind == "entries" else {}),
                **({"items": []} if kind == "list" else {}),
            }
            for order, (key, title, kind, visible) in enumerate(DEFAULT_SECTIONS)
        ],
    }


def new_entry(**fields: Any) -> dict[str, Any]:
    """One experience-shaped row, with its provenance fields present from birth.

    `source_ref` and `sync_state` are never added later: an entry without them
    is an entry the reconcile protocol cannot reason about, and retrofitting
    them means guessing where existing entries came from.
    """
    entry = {
        "id": new_id(),
        "title": "",
        "org": "",
        "dates": "",
        "bullets": [],
        "source_ref": "",
        "sync_state": SyncState.DETACHED,
    }
    entry.update({key: value for key, value in fields.items() if key in entry})
    return entry


def section_by_id(content: dict, section_id: str) -> dict | None:
    return next((s for s in content.get("sections", []) if s.get("id") == section_id), None)


def section_by_key(content: dict, key: str) -> dict | None:
    return next((s for s in content.get("sections", []) if s.get("key") == key), None)


def ensure_section(content: dict, *, key: str, title: str, kind: str) -> dict:
    """The section with this key, created and made visible if it is missing.

    `DEFAULT_SECTIONS` ships projects and certifications hidden, which is right
    for a blank CV and wrong for one built from a file that has them: a
    student's projects were extracted, had nowhere to go, and were dropped.
    Awards and publications have no default section at all, so an academic's
    CV lost its publication list on the way into the builder.
    """
    section = section_by_key(content, key)
    if section is not None:
        section["visible"] = True
        return section

    sections = content.setdefault("sections", [])
    section = {
        "id": new_id(),
        "key": key,
        "title": title,
        "kind": kind,
        "visible": True,
        "order": len(sections),
        **({"text": ""} if kind == "text" else {}),
        **({"entries": []} if kind == "entries" else {}),
        **({"items": []} if kind == "list" else {}),
    }
    sections.append(section)
    return section


def entry_by_id(content: dict, entry_id: str) -> tuple[dict, dict] | None:
    """The entry and the section holding it, or None."""
    for section in content.get("sections", []):
        for entry in section.get("entries", []) or []:
            if entry.get("id") == entry_id:
                return section, entry
    return None


def visible_sections(content: dict) -> list[dict]:
    return [s for s in content.get("sections", []) if s.get("visible")]


def describe(content: dict) -> str:
    """What is in this document, in one line a person can compare.

    The history list showed "Version 2 · Saved · 10 Sep 2026, 14:03" against
    "Version 3 · Saved · 10 Sep 2026, 14:05", which is two timestamps and no
    information: nobody can tell from that which one to bring back, and the
    only way to find out was to restore one and look. A count of roles and
    skills is not a diff, but it is the difference a candidate is usually
    looking for — "the long one, before I cut it down".
    """
    roles = sum(
        len(section.get("entries", []))
        for section in content.get("sections", [])
        if section.get("visible") and section.get("kind") == "entries"
    )
    items = sum(
        len(section.get("items", []))
        for section in content.get("sections", [])
        if section.get("visible") and section.get("kind") == "list"
    )
    sections = len(visible_sections(content))

    parts = [f"{sections} section{'' if sections == 1 else 's'}"]
    if roles:
        parts.append(f"{roles} entr{'y' if roles == 1 else 'ies'}")
    if items:
        parts.append(f"{items} listed item{'' if items == 1 else 's'}")
    return " · ".join(parts)


def content_hash(content: dict) -> str:
    """A digest of what the document *says*.

    Deliberately excludes ids and ordering metadata that do not change the
    rendered document, so that re-opening the builder and saving again does not
    look like a change (SRS §5.5.1 — analysis reuses a version whose hash
    matches).
    """
    return stable_hash(_canonical(content))


def _canonical(content: dict) -> dict:
    """Content stripped to the part a reader would notice."""
    return {
        "contact": {
            key: (content.get("contact") or {}).get(key, "")
            for key in ("name", "headline", "email", "phone", "location")
        },
        "summary": content.get("summary", ""),
        "sections": [
            {
                "key": section.get("key", ""),
                "title": section.get("title", ""),
                "kind": section.get("kind", ""),
                "text": section.get("text", ""),
                "items": list(section.get("items", []) or []),
                "entries": [
                    {
                        "title": entry.get("title", ""),
                        "org": entry.get("org", ""),
                        "dates": entry.get("dates", ""),
                        "bullets": list(entry.get("bullets", []) or []),
                    }
                    for entry in (section.get("entries", []) or [])
                ],
            }
            for section in content.get("sections", [])
            if section.get("visible")
        ],
    }


def is_empty(content: dict) -> bool:
    """Nothing worth finalizing.

    Checked before a version is created, because an empty CV finalized into an
    immutable row is a row that can never be corrected and never be deleted.
    """
    canonical = _canonical(content)
    if any(canonical["contact"].values()) or canonical["summary"].strip():
        return False
    return not any(
        section["text"].strip() or section["items"] or section["entries"]
        for section in canonical["sections"]
    )


#: What a redacted version's content is replaced by (ADR-A38). A fixed
#: tombstone rather than an empty object, so a reader of an old application
#: sees "this was erased" rather than "this was always blank".
TOMBSTONE: dict[str, Any] = {
    "schema_version": CONTENT_SCHEMA_VERSION,
    "redacted": True,
    "contact": {},
    "summary": "This document was erased at the candidate's request.",
    "sections": [],
}
