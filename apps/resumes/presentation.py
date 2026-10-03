"""
The CV as the browser draws it.

The builder's preview is the promise the export has to keep. Before this
module the preview was hand-written HTML that read the raw content dict, while
the PDF read `rendering.layout` — so a sidebar template put skills in a column
on download and left them in the middle of the page on screen, and nobody could
see it until the file arrived.

Now there is one flattening pass and one set of numbers. This module takes the
same `Document` the renderers take, and turns the theme into CSS custom
properties so the browser computes the same type sizes and the same gaps the
PDF does.

**Everything is in points, including on the web.** The preview is a picture of
a sheet of A4, and a sheet of A4 is 595.28pt wide. The stylesheet multiplies
every point value by one unit — one percent of the sheet's own width — so the
whole document scales as one object and no size is ever restated in pixels.
"""

from __future__ import annotations

import copy
from typing import Any

from . import examples
from .rendering import layout as layout_rules
from .rendering.theme import Theme, for_template

#: A4's width in PostScript points. The scale factor between the renderers'
#: units and the preview's.
A4_WIDTH_PT = 595.28


def paper(content: dict, template: Any) -> dict[str, Any]:
    """Everything a preview needs to draw one document.

    `template` may be a `ResumeTemplate` row or the snapshot dict a finalized
    version stores, so the editor and a historical version render through the
    same path.
    """
    snapshot = template.snapshot() if hasattr(template, "snapshot") else (template or {})
    theme = for_template(snapshot)
    document = layout_rules.build(content, theme=theme)

    return {
        "theme": theme,
        "paper": document,
        "paper_classes": _classes(theme),
        "paper_style": css_variables(theme),
        "paper_template": snapshot,
    }


#: The worked example the gallery draws every template with.
#:
#: **Sample data and a candidate's CV are different things, and the gallery
#: only ever shows the sample.** It used to show the candidate's most recent
#: draft instead, the moment they had one — so a candidate who opened the
#: editor, changed a line and came back found the "template preview" had
#: changed with them, and the gallery stopped being a picture of the templates
#: at all. The sample is now the only thing a gallery card, a template's page
#: or the comparison draws; a candidate's own CV appears in a template only
#: when they ask for it (`TemplateDetailView`, "Preview with my CV").
#:
#: Kept as a name because tests and screens import it. Never passed to a
#: renderer directly: `sample_content()` hands out a private copy per render,
#: so nothing downstream can write into the constant every other request reads.
SAMPLE_CONTENT: dict[str, Any] = examples.EXAMPLE_CONTENT


def sample_content() -> dict[str, Any]:
    """A private copy of the worked example, for one render."""
    return copy.deepcopy(examples.EXAMPLE_CONTENT)


def previews(templates, content: dict | None = None) -> list[dict[str, Any]]:
    """One drawable preview per template, all showing the same document.

    The whole point of a gallery is that only the layout varies, so the content
    is held constant across every card and the candidate compares the one thing
    they are actually choosing. Without `content` that document is the sample,
    copied per card so no two previews share a mutable object.
    """
    return [
        {
            "template": template,
            **paper(content if content is not None else sample_content(), template),
        }
        for template in templates
    ]


def css_variables(theme: Theme) -> str:
    """The theme as a `style` attribute for the sheet's root element.

    Emitted as an inline declaration rather than as a stylesheet per template,
    because the values are per-document: two previews of two templates appear
    side by side on the compare screen, and a stylesheet cannot tell them apart.
    """
    values = {
        "--sheet-accent": theme.accent,
        "--sheet-tint": tint(theme.accent),
        "--sheet-name-colour": theme.accent if theme.accent_name else "var(--sheet-ink)",
        "--sheet-heading-colour": theme.accent if theme.accent_headings else "var(--sheet-ink)",
        "--sheet-name": _pt(theme.name_size),
        "--sheet-headline": _pt(theme.headline_size),
        "--sheet-contact": _pt(theme.contact_size),
        "--sheet-heading": _pt(theme.heading_size),
        "--sheet-body": _pt(theme.body_size),
        "--sheet-leading": f"{theme.line_height}",
        "--sheet-margin": _mm(theme.margin_mm),
        "--sheet-sidebar": _mm(theme.sidebar_width_mm),
        "--sheet-gap": f"{theme.density}",
        "--sheet-tracking": f"{theme.heading_tracking}em",
        "--sheet-heading-case": "uppercase" if theme.heading_case == "upper" else "none",
        "--sheet-align": "center" if theme.header_align == "center" else "left",
        "--sheet-family": _family(theme.family),
    }
    return ";".join(f"{name}:{value}" for name, value in values.items())


def tint(accent: str) -> str:
    """The sidebar's wash, computed exactly as the PDF renderer computes it."""
    from .rendering.pdf import _tint

    return _tint(accent)


def _classes(theme: Theme) -> str:
    """The structural classes the stylesheet keys off.

    Structure only. Colour, size and spacing arrive as custom properties, so a
    new accent never needs a new class and the stylesheet cannot drift from the
    theme table.
    """
    parts = ["sheet", f"sheet-{theme.structure}"]
    if theme.rule_under_header:
        parts.append("sheet-ruled-head")
    if theme.rule_under_heading:
        parts.append("sheet-ruled-headings")
    if theme.sidebar_tinted:
        parts.append("sheet-tinted")
    return " ".join(parts)


def _pt(size: float) -> str:
    """A point size as a multiple of the sheet unit."""
    return f"calc({round(size / A4_WIDTH_PT * 100, 4)} * var(--sheet-unit))"


def _mm(size: float) -> str:
    return f"calc({round(size / 210 * 100, 4)} * var(--sheet-unit))"


def _family(family: str) -> str:
    return {
        "serif": "var(--font-serif, Georgia, 'Times New Roman', serif)",
        "mono": "var(--font-mono, ui-monospace, 'SF Mono', Consolas, monospace)",
    }.get(family, "var(--font-sans, Inter, system-ui, sans-serif)")
