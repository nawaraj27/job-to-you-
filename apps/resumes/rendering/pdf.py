"""
PDF rendering with ReportLab.

**Deterministic by configuration, not by luck.** A PDF normally embeds a
creation timestamp and a random file identifier, so two renders of one document
differ in bytes. `rl_config.invariant` fixes both, which is what makes the
render cache correct: the same version, template and format really do produce
the same file, and ADR-A8's "SHA-256 of the rendered file" means something.

**Pagination is the library's, not ours.** Platypus flows blocks into frames
and breaks pages where the content runs out of room, with `KeepTogether` around
the pieces that must not be split — an entry's heading away from its first
bullet is the one break that makes a CV look broken.

**The template decides the page, not this module.** Every number here comes
from `theme.py`, which the preview reads as well; the code below only turns
those numbers into frames and styles. That is what makes "what you saw in the
builder is what downloaded" a property rather than a hope.

**Reading order is the ATS's order.** In a sidebar template the name, headline
and contact line flow through a full-width frame *first*, before either column.
An applicant tracking system extracting the text therefore reads the
candidate's name before their skills list, which is the order it expects.

ReportLab's open-source build does not emit PDF/UA structure tags, so FR-3.11
(priority S, v1.1) is **not** claimed here. What is done today is the part that
actually helps a screen reader: real text in reading order, document metadata,
a language, and outline entries per section.
"""

from __future__ import annotations

import io
from typing import Any

from django.conf import settings

from .base import PDF_MIME, RenderFailed, RenderResult
from .fonts import font_set
from .layout import Block
from .theme import Theme, for_template

ENGINE = "reportlab"

#: A4 rather than Letter: the market this launches into uses A4, and a CV that
#: prints with a cut-off margin is a CV that gets thrown away.
PAGE_SIZE = "A4"

#: The gap between a sidebar and the column beside it, in millimetres.
COLUMN_GAP = 7

#: Room reserved at the foot of every page for the furniture, in millimetres.
FOOTER_BAND = 8

#: Two pages is the convention almost every employer expects. Beyond this the
#: document is not refused — it is the candidate's CV — but the count is
#: recorded so the interface can say so.
EXPECTED_MAX_PAGES = 2


def render(content: dict, *, template: dict | None = None) -> RenderResult:
    """Render one version's content to PDF bytes."""
    try:
        return _render(content, template or {})
    except RenderFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - a render must fail as a render
        raise RenderFailed(f"The PDF could not be produced: {exc}") from exc


def _render(content: dict, template: dict) -> RenderResult:  # noqa: PLR0915 - one page setup
    from reportlab import rl_config

    # Set before anything else: it governs the document id and the creation
    # date, which is the whole basis of a byte-identical re-render.
    rl_config.invariant = 1

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        FrameBreak,
        NextPageTemplate,
        PageTemplate,
    )

    from . import layout as layout_rules

    theme = for_template(template)
    document = layout_rules.build(content, theme=theme)
    fonts = font_set(template)
    styles = _styles(theme, fonts)

    buffer = io.BytesIO()
    page_width, page_height = A4
    margin = theme.margin_mm * mm
    footer_band = FOOTER_BAND * mm

    content_width = page_width - 2 * margin
    floor = margin + footer_band
    ceiling = page_height - margin

    geometry = _geometry(theme, margin, content_width, mm)

    head_flowables = _head_flowables(document, styles, theme, content_width, mm)
    head_height = _measure(head_flowables, content_width, page_height)

    class _Doc(BaseDocTemplate):
        """A document that draws its own furniture on every page."""

        def afterPage(self) -> None:  # noqa: N802 - ReportLab's hook name
            _draw_furniture(self.canv, document.title, page_width, margin, styles)

    doc = _Doc(
        buffer,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=floor,
        title=document.title,
        author=document.title,
        subject="Curriculum vitae",
        creator=settings.SITE_NAME,
        lang="en",
    )

    header_floor = ceiling - head_height

    def _panel(top: float):
        """The sidebar's tint, drawn behind the column it belongs to.

        Two callbacks rather than one because the panel stops under the header
        on page one and runs the full height afterwards: a tint drawn behind
        the candidate's own name reads as a highlighter accident.
        """

        def draw(canvas, _doc) -> None:
            if not (theme.has_sidebar and theme.sidebar_tinted):
                return
            canvas.saveState()
            canvas.setFillColor(_tint(theme.accent))
            canvas.rect(
                geometry["sidebar_x"] - 4 * mm,
                floor - 4 * mm,
                geometry["sidebar_width"] + 8 * mm,
                top - floor + 8 * mm,
                stroke=0,
                fill=1,
            )
            canvas.restoreState()

        return draw

    first_frames = [
        Frame(
            margin,
            header_floor,
            content_width,
            head_height,
            id="header",
            topPadding=0,
            bottomPadding=0,
            leftPadding=0,
            rightPadding=0,
        )
    ]
    later_frames: list[Any] = []

    def _column(x: float, width: float, top: float, name: str) -> Any:
        return Frame(
            x,
            floor,
            width,
            top - floor,
            id=name,
            topPadding=0,
            bottomPadding=0,
            leftPadding=0,
            rightPadding=0,
        )

    if theme.has_sidebar:
        first_frames.append(
            _column(geometry["sidebar_x"], geometry["sidebar_width"], header_floor, "sidebar")
        )
        first_frames.append(
            _column(geometry["body_x"], geometry["body_width"], header_floor, "body")
        )
        # Page two keeps the body column where it was and leaves the sidebar
        # empty. A CV whose text column changes width halfway through reads as
        # two documents stapled together.
        later_frames.append(_column(geometry["body_x"], geometry["body_width"], ceiling, "body"))
    else:
        first_frames.append(_column(margin, content_width, header_floor, "body"))
        later_frames.append(_column(margin, content_width, ceiling, "body"))

    doc.addPageTemplates(
        [
            PageTemplate(id="first", frames=first_frames, onPage=_panel(header_floor)),
            PageTemplate(id="later", frames=later_frames, onPage=_panel(ceiling)),
        ]
    )

    story: list[Any] = [NextPageTemplate("later"), *head_flowables, FrameBreak()]

    if theme.has_sidebar:
        for block in document.sidebar:
            story.extend(_flowables(block, styles, theme, mm, sidebar=True))
        story.append(FrameBreak())

    for block in document.band:
        story.extend(_flowables(block, styles, theme, mm, band=True))
    if document.band:
        # A rule closes the band. Without it the summary is just a slightly
        # larger first paragraph, and the structure the template is named for
        # is invisible on the page.
        story.append(_rule(None, theme, mm, weight=0.6))
        story.append(_spacer(theme.gap(3), mm))

    for block in document.body:
        story.extend(_flowables(block, styles, theme, mm))

    doc.build(story)

    data = buffer.getvalue()
    return RenderResult(
        data=data,
        mime=PDF_MIME,
        engine=ENGINE,
        pages=doc.page,
        fonts=fonts.as_dict(),
    )


def _geometry(theme: Theme, margin: float, content_width: float, mm) -> dict[str, float]:
    """Where the columns sit, in points."""
    if not theme.has_sidebar:
        return {
            "body_x": margin,
            "body_width": content_width,
            "sidebar_x": margin,
            "sidebar_width": 0.0,
        }

    sidebar_width = theme.sidebar_width_mm * mm
    body_width = content_width - sidebar_width - COLUMN_GAP * mm

    if theme.structure == "sidebar-right":
        return {
            "body_x": margin,
            "body_width": body_width,
            "sidebar_x": margin + body_width + COLUMN_GAP * mm,
            "sidebar_width": sidebar_width,
        }

    return {
        "body_x": margin + sidebar_width + COLUMN_GAP * mm,
        "body_width": body_width,
        "sidebar_x": margin,
        "sidebar_width": sidebar_width,
    }


def _measure(flowables: list[Any], width: float, height: float) -> float:
    """How tall the header block is once wrapped to the page width.

    Measured rather than guessed. A candidate with a two-line headline and a
    long address gets a header frame that fits it, and a candidate with neither
    does not get a band of white space where their first job should be.
    """
    total = 0.0
    for flowable in flowables:
        _, used = flowable.wrap(width, height)
        total += used
        total += getattr(flowable, "getSpaceBefore", lambda: 0)()
        total += getattr(flowable, "getSpaceAfter", lambda: 0)()
    return total


def _head_flowables(document, styles, theme: Theme, width: float, mm) -> list[Any]:
    """Name, headline, contact line, and the rule some templates put under it."""
    from reportlab.platypus import Paragraph

    parts: list[Any] = []
    for block in document.head:
        parts.extend(_flowables(block, styles, theme, mm))

    if theme.rule_under_header:
        parts.append(_rule(width, theme, mm, weight=0.9))
        parts.append(_spacer(theme.gap(3), mm))
    else:
        parts.append(_spacer(theme.gap(2), mm))

    # Guard against a header that is somehow empty: a frame of height zero
    # makes ReportLab refuse the whole document.
    if not parts:
        parts.append(Paragraph("&nbsp;", styles["body"]))
    return parts


def _styles(theme: Theme, fonts) -> dict[str, Any]:
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm

    align = TA_CENTER if theme.header_align == "center" else TA_LEFT
    heading_colour = theme.accent if theme.accent_headings else "#1c1a17"
    name_colour = theme.accent if theme.accent_name else "#1c1a17"

    base = ParagraphStyle(
        "body",
        fontName=fonts.regular,
        fontSize=theme.body_size,
        leading=theme.leading,
        alignment=TA_LEFT,
        textColor="#1c1a17",
    )
    return {
        "name": ParagraphStyle(
            "name",
            parent=base,
            fontName=fonts.bold,
            fontSize=theme.name_size,
            leading=theme.name_size * 1.16,
            alignment=align,
            textColor=name_colour,
            spaceAfter=theme.gap(1) * mm,
        ),
        "headline": ParagraphStyle(
            "headline",
            parent=base,
            fontSize=theme.headline_size,
            leading=theme.headline_size * 1.3,
            alignment=align,
            textColor=theme.accent,
            spaceAfter=theme.gap(1) * mm,
        ),
        "contact": ParagraphStyle(
            "contact",
            parent=base,
            fontSize=theme.contact_size,
            leading=theme.contact_size * 1.3,
            alignment=align,
            textColor="#4b5563",
            spaceAfter=theme.gap(2) * mm,
        ),
        "heading": ParagraphStyle(
            "heading",
            parent=base,
            fontName=fonts.bold,
            fontSize=theme.heading_size,
            leading=theme.heading_size * 1.25,
            textColor=heading_colour,
            spaceBefore=theme.gap(4) * mm,
            spaceAfter=theme.gap(1.5) * mm,
        ),
        "sidebar_heading": ParagraphStyle(
            "sidebar_heading",
            parent=base,
            fontName=fonts.bold,
            fontSize=theme.heading_size - 0.8,
            leading=(theme.heading_size - 0.8) * 1.25,
            textColor=heading_colour,
            spaceBefore=theme.gap(3.5) * mm,
            spaceAfter=theme.gap(1.2) * mm,
        ),
        "band": ParagraphStyle(
            "band",
            parent=base,
            fontSize=theme.body_size + 0.7,
            leading=(theme.body_size + 0.7) * theme.line_height,
            textColor="#28241f",
        ),
        "body": base,
        "sidebar_body": ParagraphStyle(
            "sidebar_body",
            parent=base,
            fontSize=theme.body_size - 0.6,
            leading=(theme.body_size - 0.6) * theme.line_height,
        ),
        "entry": ParagraphStyle(
            "entry", parent=base, fontName=fonts.bold, spaceAfter=theme.gap(0.5) * mm
        ),
        "meta": ParagraphStyle(
            "meta", parent=base, fontSize=theme.body_size - 1, textColor="#4b5563"
        ),
        # No `spaceAfter`: these paragraphs replaced the items of a ListFlowable,
        # which set none, and adding any pushed a CV that fitted on one page
        # onto two. A page break is not a cosmetic difference on a CV.
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=4 * mm, firstLineIndent=-4 * mm),
        "sidebar_bullet": ParagraphStyle(
            "sidebar_bullet",
            parent=base,
            fontSize=theme.body_size - 0.6,
            leading=(theme.body_size - 0.6) * theme.line_height,
            leftIndent=3 * mm,
            firstLineIndent=-3 * mm,
        ),
        "footer": ParagraphStyle("footer", parent=base, fontSize=7.5, textColor="#6b7280"),
        # Not a style — the list marker, which depends on the font.
        #
        # U+2022 in a base-14 Helvetica is drawn from a symbol encoding that
        # text extraction cannot map back to a character: pdfminer reads it as
        # "(cid:127)" and, worse, emits the markers detached from the lines they
        # belong to. An applicant tracking system reading an exported CV
        # therefore saw a column of unmappable glyphs followed by unbulleted
        # prose — from a product whose templates promise to "parse cleanly".
        # An en dash is in WinAnsi, extracts as U+2013 attached to its line, and
        # is a bullet style CVs have used for decades. With a real font embedded
        # the round dot maps correctly, so that is what an operator who installs
        # fonts gets.
        "bullet_char": theme.bullet if fonts.embedded else "–",
    }


def _bulleted(items, styles, style_name: str) -> list[Any]:
    """Bulleted lines whose markers survive text extraction.

    ReportLab's `ListFlowable` draws the markers in a separate text object from
    the lines they belong to, and extraction reads them in that order: an
    exported CV came out as a column of five bullets followed by five unbulleted
    sentences. Any applicant tracking system reading that file loses the list
    structure entirely, on a document this product sells as parseable.

    Putting the marker in the paragraph's own text costs one string
    concatenation and keeps each bullet attached to its line. The hanging indent
    is the paragraph style's, so wrapped lines still align under the first word.
    """
    from reportlab.platypus import Paragraph

    marker = styles["bullet_char"]
    return [
        Paragraph(f"{marker} {_escape(item)}", styles[style_name])
        for item in items
        if str(item).strip()
    ]


def _flowables(  # noqa: PLR0911 - one branch per block kind reads better than a table
    block: Block,
    styles,
    theme: Theme,
    mm,
    *,
    sidebar: bool = False,
    band: bool = False,
) -> list[Any]:
    """One layout block as ReportLab flowables."""
    from reportlab.platypus import KeepTogether, Paragraph

    if block.kind == "name":
        return [Paragraph(_escape(block.text), styles["name"])]

    if block.kind == "headline":
        return [Paragraph(_escape(block.text), styles["headline"])]

    if block.kind == "contact":
        joined = theme.contact_separator.join(_escape(item) for item in block.items)
        return [Paragraph(joined, styles["contact"])]

    if block.kind == "heading":
        style = styles["sidebar_heading" if sidebar else "heading"]
        parts: list[Any] = [Paragraph(_heading_text(block.text, theme), style)]
        if theme.rule_under_heading and not sidebar:
            parts.append(_rule(None, theme, mm, weight=0.6))
            parts.append(_spacer(theme.gap(1.2), mm))
        return parts

    if block.kind == "paragraph":
        style = styles["band"] if band else styles["sidebar_body" if sidebar else "body"]
        return [Paragraph(_escape(block.text), style), _spacer(theme.gap(1), mm)]

    if block.kind == "list":
        return [
            *_bulleted(block.items, styles, "sidebar_bullet" if sidebar else "bullet"),
            _spacer(theme.gap(1), mm),
        ]

    # An entry: heading line, dates, bullets — kept together so a role's title
    # never sits alone at the foot of a page.
    parts = []
    heading = block.text
    if block.meta.get("org"):
        heading = f"{heading} — {block.meta['org']}" if heading else block.meta["org"]
    if heading:
        parts.append(Paragraph(_escape(heading), styles["entry"]))
    if block.meta.get("dates"):
        parts.append(Paragraph(_escape(block.meta["dates"]), styles["meta"]))
    parts.extend(_bulleted(block.items, styles, "sidebar_bullet" if sidebar else "bullet"))
    parts.append(_spacer(theme.gap(2), mm))
    return [KeepTogether(parts)]


def _heading_text(text: str, theme: Theme) -> str:
    """A section heading in the case the template asks for.

    Letter-spacing is faked with hair spaces because ReportLab's paragraph
    parser has no tracking control. It is applied only to upper-case headings,
    where the effect is the difference between "SKILLS" reading as a label and
    reading as a shout.
    """
    escaped = _escape(text)
    if theme.heading_case != "upper":
        return escaped

    upper = escaped.upper()
    if theme.heading_tracking >= 0.1:
        return "&#8202;".join(upper)
    return upper


def _rule(width: float | None, theme: Theme, mm, *, weight: float):
    """A hairline the width of whatever frame it lands in."""
    from reportlab.platypus import Flowable

    colour = theme.accent if theme.accent_headings else "#c9c4bc"

    class _Rule(Flowable):
        def __init__(self) -> None:
            super().__init__()
            self.width = width or 0
            self.height = weight

        def wrap(self, available_width, _available_height):
            self.width = width or available_width
            return (self.width, self.height)

        def draw(self) -> None:
            self.canv.setStrokeColor(colour)
            self.canv.setLineWidth(weight)
            self.canv.line(0, 0, self.width, 0)

    return _Rule()


def _spacer(millimetres: float, mm):
    from reportlab.platypus import Spacer

    return Spacer(1, max(millimetres, 0.1) * mm)


def _tint(accent: str) -> str:
    """A very pale wash of the accent, for the sidebar panel.

    Computed rather than tabulated so a new accent needs no second entry, and
    kept above 92% white so that text printed on it stays legible on a laser
    printer with a tired toner cartridge.
    """
    red, green, blue = (int(accent.lstrip("#")[index : index + 2], 16) for index in (0, 2, 4))
    mix = 0.93
    blend = lambda channel: int(channel + (255 - channel) * mix)  # noqa: E731
    return f"#{blend(red):02x}{blend(green):02x}{blend(blue):02x}"


def _draw_furniture(canvas, title: str, width: float, margin: float, styles) -> None:
    """The footer every page carries: whose CV it is, and which page.

    Drawn on the canvas rather than flowed, because a footer that participates
    in the flow changes where the content breaks — and a page number that moves
    the pagination is a page number that is wrong.
    """
    canvas.saveState()
    canvas.setFont(styles["footer"].fontName, 7.5)
    canvas.setFillColor("#6b7280")
    canvas.drawString(margin, margin * 0.6, _plain(title))
    canvas.drawRightString(width - margin, margin * 0.6, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


def _escape(text: str) -> str:
    """Paragraph text is mini-HTML to ReportLab, so user text is escaped.

    A CV containing "R&D" or "<lead>" must render as those words rather than
    as a parse error or a swallowed tag.
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str) -> str:
    return (text or "").replace("\n", " ").strip()
