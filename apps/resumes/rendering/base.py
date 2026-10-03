"""
What a renderer returns, and what it may fail with.

`ENGINE_VERSION` is part of the cache key's meaning even though it is not part
of the unique constraint: a render is a pure function of (version, template,
format) **for a given engine**, so improving the layout means the old rows are
stale. `stale_for()` is how the service decides to re-render rather than serve
a file produced by a renderer that no longer exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Bump on any change that alters output bytes for the same input: layout,
#: spacing, font selection, metadata. Recorded on every `ResumeRender`.
ENGINE_VERSION = "1.2"

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class RenderFailed(Exception):
    """The document could not be rendered. Never a user's fault silently."""


class UnsupportedRenderFormat(Exception):
    """No renderer for that format."""


@dataclass(frozen=True)
class RenderResult:
    """A rendered file and everything worth recording about how it was made."""

    data: bytes
    mime: str
    engine: str
    engine_version: str = ENGINE_VERSION
    pages: int | None = None
    fonts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.data:
            # An empty file is the worst possible outcome: it downloads, opens
            # blank, and looks like the candidate's CV is empty.
            raise RenderFailed("The renderer produced an empty file.")


def stale_for(engine_version: str) -> bool:
    """Was this render produced by an older engine?"""
    return engine_version != ENGINE_VERSION
