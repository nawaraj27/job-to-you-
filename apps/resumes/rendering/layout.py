"""
One layout pass, consumed by every surface.

A CV's structure — heading, contact line, then a sequence of sections that are
prose, a list or dated entries — is the same document whether it ends up as a
PDF, a DOCX or the preview in the builder. Deciding that three times is how a
PDF and a Word file of one CV end up in different orders with different
headings, which is exactly the bug nobody notices until an employer opens the
wrong one.

So this module turns `ResumeVersion.content` into a flat list of typed blocks
grouped by *where on the page they go*, and each surface only decides how a
block looks. It has no dependency on any output library.

The grouping is the theme's call, not this module's: which sections cross into
a sidebar, and whether a summary is lifted into a full-width band, are template
decisions, and `theme.py` is where template decisions live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .theme import Theme, for_template

BlockKind = Literal["name", "headline", "contact", "heading", "paragraph", "list", "entry"]

#: A redacted version renders as its tombstone rather than as an empty page: a
#: blank PDF reads as a bug, and this reads as what happened (ADR-A38).
REDACTED_NOTICE = "This document was erased at the candidate's request."

#: Kept as a module constant because it was one: the default set now lives on
#: the theme, and this is the name callers already import.
SIDEBAR_KEYS = frozenset({"skills", "languages", "certifications", "interests"})


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    text: str = ""
    items: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Document:
    """A CV, flattened, grouped by region, and ready to draw."""

    title: str

    #: Name, headline and contact line. Every structure draws these first;
    #: they differ only in alignment and in whether a rule follows.
    head: tuple[Block, ...] = ()

    #: The full-width band a `banded` structure puts above the body. Empty for
    #: every other structure, which has nowhere to put it.
    band: tuple[Block, ...] = ()

    #: The main column.
    body: tuple[Block, ...] = ()

    #: The narrow column, when the structure has one.
    sidebar: tuple[Block, ...] = ()

    is_redacted: bool = False

    @property
    def blocks(self) -> tuple[Block, ...]:
        """Everything outside the sidebar, in reading order.

        The order a text extractor sees, which is the order that matters to an
        applicant tracking system — and the signature the renderers used before
        regions existed, so it stays.
        """
        return (*self.head, *self.band, *self.body)


def build(content: dict, *, template: dict | None = None, theme: Theme | None = None) -> Document:
    """Flatten one version's content into drawable blocks.

    Accepts a resolved `theme` so a caller that already has one — the preview,
    which needs the same theme for its CSS — does not resolve it twice.
    """
    resolved = theme or for_template(template)
    contact = content.get("contact") or {}
    name = (contact.get("name") or "").strip()

    if content.get("redacted"):
        return Document(
            title=name or "Curriculum vitae",
            head=(Block("name", text=name or "Curriculum vitae"),),
            body=(Block("paragraph", text=REDACTED_NOTICE),),
            is_redacted=True,
        )

    head: list[Block] = [Block("name", text=name or "Curriculum vitae")]
    if contact.get("headline"):
        head.append(Block("headline", text=contact["headline"]))

    details = [
        value
        for key in ("email", "phone", "location")
        if (value := (contact.get(key) or "").strip())
    ]
    if details:
        head.append(Block("contact", items=tuple(details)))

    band: list[Block] = []
    body: list[Block] = []
    sidebar: list[Block] = []

    for section in _ordered_sections(content):
        key = section.get("key")
        blocks = _section_blocks(section)
        if not blocks:
            continue

        if resolved.structure == "banded" and key == resolved.band_key and not band:
            # The band carries the argument, so it loses its heading: a
            # full-width panel labelled "Summary" is a label nobody needed.
            band.extend(block for block in blocks if block.kind != "heading")
        elif resolved.has_sidebar and key in resolved.sidebar_keys:
            sidebar.extend(blocks)
        else:
            body.extend(blocks)

    return Document(
        title=name or "Curriculum vitae",
        head=tuple(head),
        band=tuple(band),
        body=tuple(body),
        sidebar=tuple(sidebar),
    )


def _ordered_sections(content: dict) -> list[dict]:
    """Visible sections, in the order the candidate arranged them.

    Sorted by `order` rather than trusting list position: the draft service
    renumbers on every move, but a version finalized before a reorder is still
    read by its own numbering.
    """
    sections = [section for section in content.get("sections", []) or [] if section.get("visible")]
    return sorted(sections, key=lambda section: section.get("order", 0))


def _section_blocks(section: dict) -> list[Block]:
    """One section, as heading plus content. Empty sections are dropped.

    A heading with nothing under it is worse than a missing section: it reads
    as an omission on the candidate's part rather than as a section they chose
    not to fill in.
    """
    kind = section.get("kind")
    title = (section.get("title") or "").strip()

    if kind == "text":
        text = (section.get("text") or "").strip()
        if not text:
            return []
        return [Block("heading", text=title), Block("paragraph", text=text)]

    if kind == "list":
        items = tuple(
            str(item).strip() for item in (section.get("items") or []) if str(item).strip()
        )
        if not items:
            return []
        return [Block("heading", text=title), Block("list", items=items)]

    entries = [entry for entry in (section.get("entries") or []) if _entry_has_content(entry)]
    if not entries:
        return []

    blocks = [Block("heading", text=title)]
    for entry in entries:
        blocks.append(
            Block(
                "entry",
                text=(entry.get("title") or "").strip(),
                items=tuple(
                    str(bullet).strip()
                    for bullet in (entry.get("bullets") or [])
                    if str(bullet).strip()
                ),
                meta={
                    "org": (entry.get("org") or "").strip(),
                    "dates": (entry.get("dates") or "").strip(),
                },
            )
        )
    return blocks


def _entry_has_content(entry: dict) -> bool:
    return bool(
        (entry.get("title") or "").strip()
        or (entry.get("org") or "").strip()
        or [bullet for bullet in (entry.get("bullets") or []) if str(bullet).strip()]
    )
