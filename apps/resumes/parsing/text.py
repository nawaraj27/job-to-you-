"""
Bytes to text, per format.

Three readers, all pure-Python and none of which executes document content:
`pdfminer.six` for PDF, `python-docx` for OOXML, and `parsing.doc` for Word
97-2003. Writing either by hand
would be weeks of work to reproduce badly, and a PDF text layer in particular
is a genuinely hard problem that this project has no reason to solve twice.

Everything here is defensive about one thing: a file that a user uploaded is
hostile input. A malformed PDF must fail as `ExtractionFailed`, never as an
unhandled exception in a Celery worker, and never as a hang — so extraction is
bounded by page count as well as by the file-size limit `apps.files` already
enforces.
"""

from __future__ import annotations

import io
import logging
import re

from apps.files.constants import DOC, DOCX, PDF

from .base import ExtractionFailed, UnsupportedFormat

logger = logging.getLogger(__name__)

#: Beyond this, a "CV" is a portfolio or a scanned book, and reading all of it
#: costs a worker minutes for text nobody will use. The first pages are the CV.
MAX_PAGES = 30

_WHITESPACE = re.compile(r"[ \t ]+")
_BLANK_RUN = re.compile(r"\n{3,}")

#: pdfminer's stand-in for a glyph it cannot map to a character. Decorative
#: and symbol fonts hit it constantly: the bullet in a list comes through as
#: the literal text "(cid:127)". Until this was handled that string travelled
#: all the way into the extracted bullets, the AI prompt and the CV builder,
#: where a candidate read their own achievement back as
#: "(cid:127) Cut checkout latency from 2.1s to 380ms".
_CID = re.compile(r"\(cid:\d+\)")


def extract_text(data: bytes, *, mime: str) -> str:
    """Extract plain text, preserving line structure."""
    if mime == PDF:
        return _normalise(_from_pdf(data))
    if mime == DOCX:
        return _normalise(_from_docx(data))
    if mime == DOC:
        from . import doc

        return _normalise(doc.extract(data))
    raise UnsupportedFormat(f"No text reader for {mime}.")


def page_count(data: bytes, *, mime: str) -> int | None:
    """How many pages, where the format has the concept."""
    if mime != PDF:
        return None
    try:
        from pdfminer.high_level import extract_pages

        return sum(1 for _ in extract_pages(io.BytesIO(data), maxpages=MAX_PAGES + 1))
    except Exception:  # noqa: BLE001 - a page count is never worth failing over
        logger.info("page count unavailable", exc_info=True)
        return None


def _from_pdf(data: bytes) -> str:
    """A PDF's text, in the order a person reads it.

    A PDF has no lines, no columns and no reading order — only glyphs at
    coordinates. `pdfminer.extract_text` guesses an order and throws the
    coordinates away, and on anything laid out in columns the guess is wrong in
    a way nothing downstream can recover from: a designer's CV came back with
    every section label collected at the bottom of the file, detached from the
    content it labelled, and extracted to no skills, no roles and no education
    at all.

    So the layout is read instead of the text. Each line keeps its position,
    lines that share a baseline are put back on one line, and the gap between
    two fragments of a row is preserved as whitespace — which is the only
    record that a column boundary was ever there, and is what `_relayout`
    reads to split a sidebar label off its content.

    A genuinely side-by-side two-column CV is a different document and is read
    a column at a time; `_columns` decides which of the two this is. If
    anything at all goes wrong, this falls back to `extract_text`, because a
    worse reading order is recoverable and a failed upload is not.
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.high_level import extract_text as pdf_extract
    from pdfminer.pdfparser import PDFSyntaxError

    try:
        pages = list(extract_pages(io.BytesIO(data), maxpages=MAX_PAGES))
    except PDFSyntaxError as exc:
        raise ExtractionFailed("That PDF could not be read. It may be damaged.") from exc
    except Exception as exc:  # noqa: BLE001 - pdfminer raises a wide family
        # Encrypted files land here. The message is deliberately actionable:
        # "could not be read" leaves the candidate with nothing to do.
        raise ExtractionFailed(
            "That PDF could not be read. If it is password-protected, remove the "
            "password and upload it again."
        ) from exc

    try:
        return "\n\n".join(_page_text(page) for page in pages)
    except Exception:  # noqa: BLE001 - layout analysis must never lose a file
        logger.info("layout reading failed; falling back to flat extraction", exc_info=True)
        try:
            return pdf_extract(io.BytesIO(data), maxpages=MAX_PAGES)
        except Exception as exc:  # noqa: BLE001
            raise ExtractionFailed("That PDF could not be read.") from exc


#: One positioned line of text.
_Line = tuple[float, float, float, float, str]  # x0, x1, top, bottom, text


def _page_lines(page) -> list[_Line]:
    from pdfminer.layout import LTTextContainer, LTTextLine

    lines: list[_Line] = []
    for element in page:
        if not isinstance(element, LTTextContainer):
            continue
        for line in element:
            if not isinstance(line, LTTextLine):
                continue
            body = line.get_text().strip()
            if body:
                lines.append((line.x0, line.x1, line.y1, line.y0, body))
    return lines


def _page_text(page) -> str:
    lines = _page_lines(page)
    if not lines:
        return ""

    width = float(page.width) or 1.0
    split = _columns(lines, width)
    if split is not None:
        left, right = split
        return "\n".join([_rows(left, width), "", _rows(right, width)]).strip()
    return _rows(lines, width)


#: A gap in x wide enough to be a column boundary rather than a word space.
MIN_GUTTER = 0.025

#: Neither side of a real two-column layout is a handful of short labels.
MIN_COLUMN_LINES = 5
MIN_COLUMN_SHARE = 0.25


def _columns(lines: list[_Line], width: float) -> tuple[list[_Line], list[_Line]] | None:
    """The page's two columns, if it genuinely has two.

    A band of x that no line crosses is a candidate boundary. That alone is not
    enough: a sidebar of four section labels also leaves one, and reading that
    page a column at a time puts every label first and every piece of content
    after — which is precisely the failure this whole function exists to avoid.
    So both sides have to hold a real share of the page.
    """
    if len(lines) < MIN_COLUMN_LINES * 2:
        return None

    spans = sorted((line[0], line[1]) for line in lines)
    reach = spans[0][1]
    for start, end in spans[1:]:
        if start - reach >= MIN_GUTTER * width and reach > 0.15 * width:
            break
        reach = max(reach, end)
    else:
        return None

    boundary = reach
    left = [line for line in lines if line[1] <= boundary]
    right = [line for line in lines if line[0] > boundary]
    if len(left) + len(right) != len(lines):
        return None

    smaller = min(len(left), len(right))
    if smaller < MIN_COLUMN_LINES or smaller / len(lines) < MIN_COLUMN_SHARE:
        return None
    return left, right


def _rows(lines: list[_Line], width: float) -> str:
    """Lines in reading order, with the gaps inside a row kept as whitespace."""
    if not lines:
        return ""

    heights = sorted(line[2] - line[3] for line in lines if line[2] > line[3])
    tolerance = (heights[len(heights) // 2] if heights else 10.0) * 0.5

    ordered = sorted(lines, key=lambda line: (-line[2], line[0]))
    rows: list[list[_Line]] = []
    for line in ordered:
        if rows and abs(rows[-1][0][2] - line[2]) <= tolerance:
            rows[-1].append(line)
        else:
            rows.append([line])

    out: list[str] = []
    previous_bottom: float | None = None
    for row in rows:
        row.sort(key=lambda line: line[0])
        if previous_bottom is not None and previous_bottom - row[0][2] > tolerance * 2.5:
            out.append("")
        out.append(_join_row(row, width))
        previous_bottom = min(line[3] for line in row)
    return "\n".join(out)


def _join_row(row: list[_Line], width: float) -> str:
    """One row of a page, with a column gap written as a gap.

    Four spaces rather than one, because that is the difference between "the
    label beside this paragraph" and "the first two words of this paragraph",
    and it is the only difference a reader further down the pipeline can see.
    """
    parts = [row[0][4]]
    for previous, current in zip(row, row[1:], strict=False):
        gap = current[0] - previous[1]
        parts.append("    " if gap >= MIN_GUTTER * width else " ")
        parts.append(current[4])
    return "".join(parts)


def _from_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx raises a wide family
        # BadZipFile, PackageNotFoundError, KeyError on a missing part: a user
        # file is hostile input and every one of these means the same thing.
        raise ExtractionFailed("That Word file could not be read. It may be damaged.") from exc

    return "\n".join(_docx_body(document))


def _docx_body(document) -> list[str]:
    """Every block in the order the document writes it.

    Tables are where a third of real CVs keep their dates and employers, so a
    reader that skips them reports "no experience found" on a document that is
    nothing but experience. But reading the paragraphs first and appending the
    tables afterwards is worse than skipping them: it moves the employment
    history to the bottom of the text, underneath whatever heading happens to
    come last. A Word CV with its roles in a table had its entire career read
    as a list of skills, because "Skills" was the final paragraph heading and
    the table landed after it.

    So the body is walked element by element. `python-docx` exposes the two
    collections separately and the XML in order; this is the only place that
    difference matters, and it matters completely.
    """
    import docx.table
    import docx.text.paragraph

    lines: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            lines.append(_docx_paragraph(docx.text.paragraph.Paragraph(child, document)))
        elif tag == "tbl":
            lines.extend(_docx_table(docx.table.Table(child, document)))
    return lines


#: The character a list paragraph gets back.
LIST_MARKER = "• "


def _docx_paragraph(paragraph) -> str:
    """One paragraph, with its list marker restored.

    Word does not put a bullet character in the text. It applies a list style
    and draws the bullet, so `paragraph.text` for a bulleted line is the line
    with no marker on it at all — and every reader downstream that asks "is
    this a bullet?" gets no for a document that is nothing but bullets.

    On a Word CV that cost a student their projects: the lines under "Notes
    API" were its two bullets, arrived unmarked, and each was read as a
    separate project. Putting the character back is restoring information the
    file has and the text layer drops, not guessing at one.
    """
    text = paragraph.text.strip()
    if not text:
        return paragraph.text

    try:
        style = (paragraph.style.name or "").lower()
    except (AttributeError, KeyError):  # pragma: no cover - malformed styles
        return paragraph.text

    if "list" in style and "number" not in style:
        return LIST_MARKER + text
    return paragraph.text


#: What separates two cells of a table row once it is one line of text.
#:
#: A separator, not two spaces. The cells of a row are different facts — a job
#: title, an employer, a date range — and joining them with whitespace makes
#: them one: "Primary School Teacher Rato Bangala School" reached a candidate's
#: profile as a job title, with no employer beside it, because nothing left in
#: the line said where one ended and the next began. The bar is already one of
#: the separators every splitter downstream understands.
CELL_JOIN = " | "


def _docx_table(table) -> list[str]:
    """One line per row, cells joined.

    Duplicate cell text is dropped because a merged cell is reported once per
    column it spans, and "2021 - Present | 2021 - Present" is not what anybody
    wrote.
    """
    lines = []
    for row in table.rows:
        try:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
        except IndexError:  # pragma: no cover - malformed merge in a user file
            continue
        if cells:
            lines.append(CELL_JOIN.join(dict.fromkeys(cells)))
    return lines


def _normalise(text: str) -> str:
    """Collapse the whitespace noise both readers produce, keep the lines.

    The order matters. Everything that reads *structure out of* whitespace has
    to run before the whitespace is collapsed, because a PDF's only record of a
    column gutter or a letter-spaced title is the run of spaces itself. Doing
    it the other way round is why a designer's CV read as one unbroken line
    with no headings in it at all.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_cids(text)
    text = _repair_ligatures(text)
    text = "\n".join(_relayout(line) for line in text.split("\n"))
    text = text.replace("•", "\n• ").replace("", "\n• ")
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip()


#: The typographic ligatures a PDF font substitutes for a pair of letters.
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})

#: A ligature standing alone between two spaces was never a pair of letters —
#: no word is "fi" — so it is a glyph the font mapped wrongly, and in a CV the
#: character in that position is almost always the dash of a date range. Left
#: as it was, "2022 ﬁ now" is not a date range, and a designer with six years
#: of work had both of her roles read as nothing at all.
_LONE_LIGATURE = re.compile(r"(?<=\s)[ﬀ-ﬆ](?=\s)")

#: A run of spaces wide enough to be a column gutter rather than a word space.
_GUTTER = re.compile(r"\s{3,}")

#: Where a sidebar label ends and the column beside it begins.
MAX_LABEL_WORDS = 3

#: A right-hand column that is nothing but dates. Designers set a job title
#: hard left and its dates hard right on the same baseline, which leaves the
#: same wide gap a sidebar label does — so without this test "Product Designer
#: [gap] 2019 – 2022" split in two, "Product Designer" then read as a section
#: heading, and the role under it was lost along with the rest of the section.
_DATES_ONLY = re.compile(
    r"^[\d\s–—/.,()·|-]*"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"present|now|current|ongoing|to|until)?"
    r"[\d\s–—/.,()·|-]*$",
    re.IGNORECASE,
)


def _repair_ligatures(text: str) -> str:
    if not any(character in text for character in "ﬀﬁﬂﬃﬄ"):
        return text
    return _LONE_LIGATURE.sub("–", text).translate(_LIGATURES)


def _relayout(line: str) -> str:
    """Recover the lines a two-column layout flattened into one.

    A PDF has no columns, only glyphs at positions, and pdfminer writes a row
    of a two-column page as one line with the gutter left in as whitespace:

        WORK               Lead Product Designer            2022 - now

    Collapsing that to single spaces — which is what used to happen first —
    leaves "WORK Lead Product Designer 2022 - now", in which "WORK" is not a
    heading, nothing starts the experience section, and the candidate is told
    their CV has no work history. Splitting at the gutter puts the label back
    on its own line, and costs nothing on a single-column document, which has
    no gutter to split at.

    Only a short, heading-shaped left-hand side splits. A wide gap in the
    middle of a role line — between the job title and its dates, which is the
    other place designers put one — stays on one line, because that is one fact
    written with space in it.
    """
    unspaced = _unspace(line)
    if unspaced != line:
        return unspaced

    # On the indent, not in it. A page with a left margin opens every line with
    # a run of spaces, and searching the raw line finds that one first: the
    # label came out empty and every sidebar CV was left exactly as it was.
    body = line.lstrip()
    match = _GUTTER.search(body)
    if match is None:
        return line

    label = body[: match.start()].strip()
    rest = body[match.end() :].strip()
    if not label or not rest or len(label.split()) > MAX_LABEL_WORDS:
        return line
    if any(character.isdigit() for character in label):
        return line
    if _DATES_ONLY.match(rest):
        return line

    letters = [character for character in label if character.isalpha()]
    if not letters:
        return line
    if not (all(character.isupper() for character in letters) or label.istitle()):
        return line

    return label + "\n" + rest


def _unspace(line: str) -> str:
    """Put a letter-spaced title back together.

    "A N J A L I   K A R K I" is how a designer sets their own name, and it is
    a name — not eleven one-letter words. The word boundaries are the wider
    gaps, which is the same information the gutter split uses and the same
    reason this has to run before the whitespace is collapsed.
    """
    words = [part for part in _GUTTER.split(line.strip()) if part]
    if len(words) < 2:
        return line

    rebuilt = []
    for word in words:
        tokens = word.split()
        if len(tokens) < 2 or not all(len(token) == 1 for token in tokens):
            return line
        rebuilt.append("".join(tokens))
    return " ".join(rebuilt)


def _strip_cids(text: str) -> str:
    """Turn pdfminer's unmapped-glyph markers into something readable.

    A marker that opens a line was a list bullet in the original document, so
    it becomes one. Anywhere else it stood for a character nobody can recover,
    and an empty string is a better guess than "(cid:3)" in the middle of a
    candidate's sentence.
    """
    if "(cid:" not in text:
        return text

    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if _CID.match(stripped):
            line = "\u2022 " + _CID.sub("", stripped).lstrip()
        lines.append(_CID.sub("", line))
    return "\n".join(lines)
