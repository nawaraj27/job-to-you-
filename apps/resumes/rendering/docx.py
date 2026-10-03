"""
DOCX rendering with python-docx.

Word is not a second-class output here. Plenty of employers and every second
recruitment agency ask for a Word file, and a candidate who has to convert one
by hand ends up sending something that looks nothing like the PDF.

**Single column, always — and that is a decision, not a limitation.** Word can
draw a sidebar with a two-column table, and a good number of commercial CV
builders do exactly that. Every widely deployed applicant tracking system reads
a table by walking cells, which turns a sidebar CV into a skills list wedged
into the middle of a job description. The PDF gets the column because a PDF is
read by a human and extracted as a text stream in frame order; the DOCX stays
linear because a DOCX is read by the parser first. The exports screen says so
rather than letting the candidate discover it.

What the DOCX *does* keep from the template is everything that survives being
linear: the typeface, the accent, the heading case and rules, the header
alignment, and the density. A Meridian and an Atlas Word file do not look the
same.

**Deterministic**, like the PDF path: python-docx writes a zip, and a zip
records a modification time per entry, so the times are normalised after the
document is built. Without that, two renders of one CV differ in bytes and the
cache is silently useless.

Styling is applied to runs rather than by defining custom Word styles: a style
a recipient's Word does not have degrades to something arbitrary, while direct
formatting travels.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from .base import DOCX_MIME, RenderFailed, RenderResult
from .layout import Block
from .theme import Theme, accent_rgb, for_template

ENGINE = "python-docx"

#: A fixed timestamp for every zip entry. Any constant works; this one is the
#: usual "zero" for reproducible builds.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

#: Word needs a real font name, and the base-14 families are not it. These are
#: installed on essentially every machine that opens a .docx.
WORD_FONTS = {"sans": "Calibri", "serif": "Cambria", "mono": "Consolas"}

#: The bullet Word uses for a list item. Unlike the PDF path there is no font
#: substitution question here: every machine that opens a .docx has a font with
#: U+2022 in it.
GREY = (0x4B, 0x55, 0x63)


def render(content: dict, *, template: dict | None = None) -> RenderResult:
    try:
        return _render(content, template or {})
    except RenderFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - a render must fail as a render
        raise RenderFailed(f"The Word file could not be produced: {exc}") from exc


def _render(content: dict, template: dict) -> RenderResult:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    from . import layout as layout_rules

    theme = for_template(template)
    document_model = layout_rules.build(content, theme=theme)
    font_name = WORD_FONTS.get(theme.family, "Calibri")
    accent = accent_rgb(theme)

    word = docx.Document()
    _page_setup(word, theme)

    normal = word.styles["Normal"]
    normal.font.name = font_name
    normal.font.size = Pt(theme.body_size + 0.5)

    tools = _Tools(
        word=word,
        theme=theme,
        font_name=font_name,
        accent=accent,
        Pt=Pt,
        RGBColor=RGBColor,
        align=WD_ALIGN_PARAGRAPH,
    )

    for block in document_model.head:
        _write(tools, block, region="head")
    if theme.rule_under_header:
        _rule(tools, weight=8)

    for block in document_model.band:
        _write(tools, block, region="band")
    if document_model.band:
        _rule(tools, weight=6)

    for block in document_model.body:
        _write(tools, block, region="body")

    # The sidebar's blocks are appended after the body rather than placed in a
    # column, for the reason in this module's docstring. They keep their own
    # headings, so a reader sees "Skills" where they expect it.
    for block in document_model.sidebar:
        _write(tools, block, region="body")

    _footer(word, document_model.title, Pt=Pt)

    buffer = io.BytesIO()
    word.save(buffer)

    return RenderResult(
        data=_normalise(buffer.getvalue()),
        mime=DOCX_MIME,
        engine=ENGINE,
        pages=None,  # Word paginates at open time; claiming a count would be a guess.
        fonts={"family": theme.family, "regular": font_name, "embedded": "no"},
    )


class _Tools:
    """The handful of python-docx names the writers need, carried together.

    Passed as one object rather than seven keyword arguments: the imports are
    deliberately local to `_render` so that importing this module does not pull
    in python-docx, and threading them through by hand made every writer
    signature longer than its body.
    """

    def __init__(self, **names: Any) -> None:
        self.__dict__.update(names)

    def __getattr__(self, name: str) -> Any:
        """Declared for the type checker, never reached for a name that is set.

        The names arrive as keywords and land in `__dict__`, so there is
        nothing static for a checker to see and every use of `tools.Pt` read as
        an error. Raising keeps the runtime behaviour of a missing attribute
        exactly as it was.
        """
        raise AttributeError(name)


def _page_setup(word, theme: Theme) -> None:
    from docx.shared import Mm

    for section in word.sections:
        section.page_height = Mm(297)
        section.page_width = Mm(210)
        for attribute in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
            setattr(section, attribute, Mm(theme.margin_mm))


def _write(tools: _Tools, block: Block, *, region: str) -> None:  # noqa: PLR0911
    theme: Theme = tools.theme
    Pt = tools.Pt  # noqa: N806
    RGBColor = tools.RGBColor  # noqa: N806

    if block.kind == "name":
        paragraph = tools.word.add_paragraph()
        paragraph.alignment = _alignment(tools, theme.header_align)
        run = paragraph.add_run(block.text)
        run.bold = True
        run.font.size = Pt(theme.name_size)
        run.font.name = tools.font_name
        if theme.accent_name:
            run.font.color.rgb = RGBColor(*tools.accent)
        return

    if block.kind == "headline":
        paragraph = tools.word.add_paragraph()
        paragraph.alignment = _alignment(tools, theme.header_align)
        run = paragraph.add_run(block.text)
        run.font.size = Pt(theme.headline_size)
        run.font.color.rgb = RGBColor(*tools.accent)
        return

    if block.kind == "contact":
        paragraph = tools.word.add_paragraph()
        paragraph.alignment = _alignment(tools, theme.header_align)
        run = paragraph.add_run(theme.contact_separator.join(block.items))
        run.font.size = Pt(theme.contact_size)
        run.font.color.rgb = RGBColor(*GREY)
        return

    if block.kind == "heading":
        paragraph = tools.word.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(theme.gap(10))
        paragraph.paragraph_format.space_after = Pt(theme.gap(2))
        text = block.text.upper() if theme.heading_case == "upper" else block.text
        run = paragraph.add_run(text)
        run.bold = True
        run.font.size = Pt(theme.heading_size)
        if theme.accent_headings:
            run.font.color.rgb = RGBColor(*tools.accent)
        if theme.rule_under_heading:
            _border(
                paragraph, weight=6, colour=_hex(tools.accent if theme.accent_headings else GREY)
            )
        return

    if block.kind == "paragraph":
        paragraph = tools.word.add_paragraph()
        run = paragraph.add_run(block.text)
        if region == "band":
            run.font.size = Pt(theme.body_size + 1.2)
        return

    if block.kind == "list":
        for item in block.items:
            tools.word.add_paragraph(item, style="List Bullet")
        return

    heading = block.text
    if block.meta.get("org"):
        heading = f"{heading} — {block.meta['org']}" if heading else block.meta["org"]
    if heading:
        paragraph = tools.word.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(theme.gap(6))
        paragraph.paragraph_format.space_after = Pt(0)
        run = paragraph.add_run(heading)
        run.bold = True
    if block.meta.get("dates"):
        paragraph = tools.word.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(theme.gap(2))
        run = paragraph.add_run(block.meta["dates"])
        run.font.size = Pt(theme.body_size - 0.5)
        run.font.color.rgb = tools.RGBColor(*GREY)
    for bullet in block.items:
        tools.word.add_paragraph(bullet, style="List Bullet")


def _alignment(tools: _Tools, where: str):
    return tools.align.CENTER if where == "center" else tools.align.LEFT


def _rule(tools: _Tools, *, weight: int) -> None:
    """A horizontal rule, as an empty paragraph carrying a bottom border.

    Word has no rule element of its own; this is what every template that draws
    one actually does.
    """
    theme: Theme = tools.theme
    paragraph = tools.word.add_paragraph()
    paragraph.paragraph_format.space_before = tools.Pt(theme.gap(2))
    paragraph.paragraph_format.space_after = tools.Pt(theme.gap(4))
    _border(paragraph, weight=weight, colour=_hex(tools.accent))


def _border(paragraph, *, weight: int, colour: str) -> None:
    from docx.oxml.ns import qn
    from docx.oxml.parser import OxmlElement

    properties = paragraph._p.get_or_add_pPr()  # noqa: SLF001 - python-docx has no public API
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(weight))
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), colour)
    borders.append(bottom)
    properties.append(borders)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "".join(f"{channel:02X}" for channel in rgb)


def _footer(word, title: str, *, Pt) -> None:  # noqa: N803
    """Whose CV this is, on every page. Word supplies the page number field."""
    for section in word.sections:
        paragraph = section.footer.paragraphs[0]
        paragraph.text = title
        for run in paragraph.runs:
            run.font.size = Pt(8)


def _normalise(data: bytes) -> bytes:
    """Rewrite the zip with fixed timestamps so the bytes are reproducible.

    Two renders of one document must hash the same, or the cache is a lie and
    ADR-A8's "hash of the file submitted" cannot be checked.
    """
    source = io.BytesIO(data)
    target = io.BytesIO()

    with (
        zipfile.ZipFile(source) as original,
        zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as rebuilt,
    ):
        for name in sorted(original.namelist()):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            rebuilt.writestr(info, original.read(name))

    return target.getvalue()
