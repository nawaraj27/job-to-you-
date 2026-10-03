"""
Font management for rendering.

Two facts shape this:

  * **A CV must render on any machine that runs the container.** Depending on
    fonts installed on the host produces a document that looks different in
    production from staging, which is the failure a candidate discovers by
    posting the wrong-looking file to an employer.
  * **Font files are licensed.** Shipping a TTF in the repository is a
    licensing decision, not a technical one, so this project does not make it
    silently.

So: fonts are loaded from `settings.RENDER_FONT_DIR` if an operator puts them
there, and otherwise rendering falls back to the PDF base-14 families, which
every reader has and nobody has to license. **Which font was actually used is
recorded on every render**, so a document that looks different from last month
is explicable rather than mysterious.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

#: The base-14 families a PDF reader is required to have. Always available,
#: never embedded, and the reason a render never fails for want of a font.
BUILTIN = {
    "serif": ("Times-Roman", "Times-Bold", "Times-Italic"),
    "sans": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique"),
    "mono": ("Courier", "Courier-Bold", "Courier-Oblique"),
}

#: Template `font` values mapped onto a family. A template naming a font nobody
#: installed still renders, in the closest builtin, and says so.
FAMILY_ALIASES = {
    "source serif": "serif",
    "times": "serif",
    "georgia": "serif",
    "inter": "sans",
    "helvetica": "sans",
    "arial": "sans",
    "roboto": "sans",
    "courier": "mono",
}

#: Files an operator may drop into RENDER_FONT_DIR, per family. Regular, bold
#: and italic, because a CV without bold headings is a CV nobody can scan.
INSTALLABLE = {
    "sans": ("Inter-Regular.ttf", "Inter-Bold.ttf", "Inter-Italic.ttf"),
    "serif": ("SourceSerif-Regular.ttf", "SourceSerif-Bold.ttf", "SourceSerif-Italic.ttf"),
}


@dataclass(frozen=True)
class FontSet:
    """The three faces one render will use, and whether they are embedded."""

    regular: str
    bold: str
    italic: str
    embedded: bool
    family: str

    def as_dict(self) -> dict[str, str]:
        """What gets stored on the render row."""
        return {
            "family": self.family,
            "regular": self.regular,
            "bold": self.bold,
            "italic": self.italic,
            "embedded": "yes" if self.embedded else "no",
        }


def family_for(template: dict | None) -> str:
    """Which family a template asks for, normalised."""
    name = ((template or {}).get("font") or "").strip().lower()
    return FAMILY_ALIASES.get(name, "sans")


def font_set(template: dict | None) -> FontSet:
    """Resolve a template's font to three usable faces.

    Registration is cached per family: `registerFont` mutates a global registry
    in ReportLab, and doing it per render would grow that registry once per
    exported CV.
    """
    family = family_for(template)
    installed = _register(family)
    if installed is not None:
        return installed

    regular, bold, italic = BUILTIN[family]
    return FontSet(regular, bold, italic, embedded=False, family=family)


@lru_cache(maxsize=8)
def _register(family: str) -> FontSet | None:
    """Register a family's TTFs, or None if they are not installed."""
    directory = getattr(settings, "RENDER_FONT_DIR", None)
    names = INSTALLABLE.get(family)
    if not directory or names is None:
        return None

    root = Path(directory)
    paths = [root / name for name in names]
    if not all(path.exists() for path in paths):
        return None

    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        faces = []
        for path in paths:
            face = path.stem
            pdfmetrics.registerFont(TTFont(face, str(path)))
            faces.append(face)

        pdfmetrics.registerFontFamily(faces[0], normal=faces[0], bold=faces[1], italic=faces[2])
        return FontSet(faces[0], faces[1], faces[2], embedded=True, family=family)
    except Exception:  # noqa: BLE001 - a bad font file must not stop an export
        logger.warning("font family %r could not be registered", family, exc_info=True)
        return None


def reset_cache() -> None:
    """Forget registered families. For tests that install fonts mid-run."""
    _register.cache_clear()
