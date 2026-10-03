"""
Is this file a CV we can read? Decided before anything is stored.

Every check here runs synchronously, on the request, before `apps.files.store`
writes a byte or `ResumeUpload` writes a row. A refused file therefore leaves
nothing behind: no attachment, no upload, no parse job, no analysis. That is
the point of doing it here rather than letting the parse fail later — a failed
parse is a row the candidate has to look at and delete; a refusal is a message.

The checks, in order, each with its own reason:

  1. **Size** — empty, or over `FILES_MAX_UPLOAD_BYTES`.
  2. **Name** — the extension must be .pdf, .doc or .docx. Anything else is
     named back to the person ("that is a spreadsheet").
  3. **Bytes** — the content must actually be that format. A PNG renamed
     .pdf, a ZIP renamed .docx, a macro-enabled .docm renamed .docx and an
     executable renamed anything are all caught here, by signature and, for
     Word files, by opening the container.
  4. **Structure** — a .docx must be a well-formed Word document (main part of
     the right content type, no macros, no DOCTYPE, within zip-bomb limits); a
     .doc must be a Word 97-2003 compound file that is not encrypted; a PDF
     must open, not be password-locked, and have at least one page.
  5. **Content** (signed-in uploads only) — the text is extracted with the same
     reader the parser uses, and the deterministic CV reader decides whether
     it is a CV: a scan with no text layer, a near-empty file, or a document
     with no CV sections and no dated roles or studies is refused.

Step 5 is skipped for guest uploads. ADR-A32: nothing reads a guest's file
contents until an account claims it, and that includes this gate. Steps 1-4
read structure, not content, and still run.

The extracted text is returned so the parse does not read the file again
(`services.upload` hands it on).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import IO

from django.conf import settings
from django.core.files import File

from apps.files import sniff
from apps.files.constants import DOC, DOCX, PDF
from apps.files.services import UnsupportedFileType

#: The sentence every refusal starts with.
HEADLINE = "Upload your CV or resume in PDF, DOC, or DOCX format."

ACCEPTED_EXTENSIONS = {".pdf": PDF, ".doc": DOC, ".docx": DOCX}

#: Zip-bomb limits for a .docx. A real CV is a few hundred KB unpacked and a
#: few dozen parts; these are two orders of magnitude above that.
MAX_ZIP_ENTRIES = 1000
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_PART_RATIO = 200
#: More pages than any CV has; `parsing.text.MAX_PAGES` reads at most 30.
MAX_PDF_PAGES = 100

_KIND_REASONS = {
    "image": "That file is an image. A photo or screenshot of a CV can't be read; save or export the CV itself as a PDF.",
    "zip": "That file is a compressed archive (ZIP). Upload the CV inside it instead.",
    "archive": "That file is a compressed archive. Upload the CV inside it instead.",
    "program": "That file is a program, not a document.",
    "html": "That file is a web page (HTML). Save the CV as a PDF and upload that.",
    "svg": "That file is an SVG image, not a document.",
    "xml": "That file is an XML data file, not a document.",
    "csv": "That file is a spreadsheet (CSV), not a CV.",
    "spreadsheet": "That file is a spreadsheet, not a CV.",
    "presentation": "That file is a presentation, not a CV.",
    "rtf": "That file is a Rich Text (RTF) document. Open it in Word and save it as DOCX or PDF.",
    "text": "That file is plain text. Save the CV as a PDF or Word document and upload that.",
    "opendocument": "That file is an OpenDocument file. Save it as DOCX or PDF and upload that.",
    "docm": "That file is a macro-enabled Word document. Save it as a normal Word document (.docx) or as a PDF.",
    "dotx": "That file is a Word template. Open it, save it as a normal document (.docx) or as a PDF.",
    "encrypted-office": "That Word file is password-protected. Remove the password and upload it again.",
}

_EXTENSION_REASONS = {
    (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"): "image",
    (".zip", ".rar", ".7z", ".gz", ".tar", ".tgz", ".bz2"): "archive",
    (
        ".exe",
        ".msi",
        ".bat",
        ".cmd",
        ".com",
        ".dll",
        ".apk",
        ".app",
        ".sh",
        ".js",
        ".jar",
        ".scr",
        ".ps1",
        ".vbs",
    ): "program",
    (".html", ".htm", ".xhtml", ".mht", ".mhtml"): "html",
    (".svg",): "svg",
    (".xml", ".json", ".yaml", ".yml"): "xml",
    (".csv", ".tsv"): "csv",
    (".xls", ".xlsx", ".xlsm", ".ods", ".numbers"): "spreadsheet",
    (".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".key"): "presentation",
    (".rtf",): "rtf",
    (".txt", ".md", ".text"): "text",
    (".odt", ".pages", ".wpd"): "opendocument",
    (".docm",): "docm",
    (".dot", ".dotx", ".dotm"): "dotx",
}

_YEAR_RANGE = re.compile(
    r"\b(?:19|20)\d{2}\s*(?:[-–—]|to|until)\s*(?:(?:19|20)\d{2}|present|now|current|date)\b",
    re.IGNORECASE,
)


class CvRejected(UnsupportedFileType):
    """A file refused as a CV, with the reason a person can act on."""

    code = "cv_rejected"

    def __init__(self, reason: str) -> None:
        super().__init__(f"{HEADLINE} {reason}")
        self.reason = reason
        self.headline = HEADLINE


@dataclass(frozen=True)
class CheckedCv:
    media_type: str
    #: Extracted text, when the content was read (signed-in uploads).
    text: str | None = None
    pages: int | None = None


def check(uploaded_file: File, *, read_content: bool) -> CheckedCv:
    """Validate an upload as a CV, or raise `CvRejected` (or `FileTooLarge`)."""
    from apps.files.services import FileTooLarge

    size = uploaded_file.size or 0
    if size <= 0:
        raise CvRejected("That file is empty.")
    limit = settings.FILES_MAX_UPLOAD_BYTES
    if size > limit:
        raise FileTooLarge(
            f"{HEADLINE} That file is larger than the {limit // (1024 * 1024)} MB limit."
        )

    stream: IO[bytes] | None = uploaded_file.file
    if stream is None:
        raise CvRejected("That file could not be read.")

    name = (uploaded_file.name or "").strip()
    extension = _extension(name)
    if extension not in ACCEPTED_EXTENSIONS:
        raise CvRejected(_reason_for_extension(extension))

    stream.seek(0)
    kind = sniff.classify(stream)
    stream.seek(0)
    expected = ACCEPTED_EXTENSIONS[extension]
    actual = {"pdf": PDF, "docx": DOCX, "doc": DOC}.get(kind)
    if actual is None:
        reason = _KIND_REASONS.get(kind, "That file isn't a PDF or Word document.")
        if kind in _KIND_REASONS and kind not in ("docm", "dotx", "encrypted-office"):
            reason = f"It is named {extension} but its contents are not. " + reason
        raise CvRejected(reason)
    if actual != expected:
        raise CvRejected(
            f"It is named {extension} but is actually a {_label(actual)} file. "
            "Rename it to match and try again."
        )

    data = stream.read()
    stream.seek(0)
    pages = None
    if actual == DOCX:
        _check_docx(data)
    elif actual == DOC:
        _check_doc(data)
    else:
        pages = _check_pdf(data)

    if not read_content:
        return CheckedCv(media_type=actual, pages=pages)

    text = _readable_text(data, actual)
    _check_is_a_cv(text, actual)
    return CheckedCv(media_type=actual, text=text, pages=pages)


# --- names -------------------------------------------------------------------


def _extension(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[1].lower()


def _reason_for_extension(extension: str) -> str:
    if not extension:
        return "That file has no extension, so its type can't be checked."
    for extensions, kind in _EXTENSION_REASONS.items():
        if extension in extensions:
            return _KIND_REASONS[kind]
    return f"Files ending in {extension} aren't accepted."


def _label(media_type: str) -> str:
    return {PDF: "PDF", DOCX: "Word (.docx)", DOC: "Word 97-2003 (.doc)"}[media_type]


# --- structure ---------------------------------------------------------------


def _check_docx(data: bytes) -> None:
    corrupt = "That Word file is damaged and can't be opened."
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise CvRejected(corrupt) from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise CvRejected("That Word file has an unusual structure and can't be read safely.")
        unpacked = sum(info.file_size for info in infos)
        if unpacked > MAX_UNPACKED_BYTES:
            raise CvRejected(
                "That Word file unpacks to an unreasonable size and can't be read safely."
            )
        for info in infos:
            if (
                info.file_size > 1024 * 1024
                and info.compress_size
                and (info.file_size / info.compress_size > MAX_PART_RATIO)
            ):
                raise CvRejected(
                    "That Word file has an unusual structure and can't be read safely."
                )
        names = {info.filename for info in infos}
        if "word/vbaProject.bin" in names:
            raise CvRejected(_KIND_REASONS["docm"])
        try:
            types = archive.read("[Content_Types].xml")
            body = archive.read("word/document.xml")
            if archive.testzip() is not None:
                raise CvRejected(corrupt)
        except (KeyError, zipfile.BadZipFile, RuntimeError, OSError, EOFError):
            raise CvRejected(corrupt) from None

    if b"wordprocessingml.document.main+xml" not in types:
        raise CvRejected(corrupt)
    for part in (types, body):
        head = part[:4096].upper()
        if b"<!DOCTYPE" in head or b"<!ENTITY" in part.upper():
            raise CvRejected("That Word file contains content that can't be read safely.")
    try:
        from xml.etree import ElementTree

        ElementTree.fromstring(body)  # noqa: S314 - DTDs refused above; no entities
    except ElementTree.ParseError:
        raise CvRejected(corrupt) from None
    try:
        import docx

        docx.Document(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - python-docx raises a wide family
        raise CvRejected(corrupt) from None


def _check_doc(data: bytes) -> None:
    from apps.files.ole import NotCompoundFile, open_compound

    try:
        compound = open_compound(data)
    except NotCompoundFile:
        raise CvRejected("That Word file is damaged and can't be opened.") from None
    if not compound.has_stream("WordDocument"):
        raise CvRejected("That file is not a Word document.")
    names = {name.lower() for name in compound.entry_names()}
    if "macros" in names or "_vba_project" in names or "vba" in names:
        raise CvRejected(
            "That Word file contains macros. Save it as a normal Word document (.docx) or as a PDF."
        )
    from apps.resumes.parsing.doc import is_encrypted

    try:
        encrypted = is_encrypted(compound)
    except (NotCompoundFile, KeyError, ValueError):
        raise CvRejected("That Word file is damaged and can't be opened.") from None
    if encrypted:
        raise CvRejected(_KIND_REASONS["encrypted-office"])


def _check_pdf(data: bytes) -> int:
    from pdfminer.pdfdocument import PDFDocument, PDFEncryptionError, PDFPasswordIncorrect
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser, PDFSyntaxError

    try:
        parser = PDFParser(io.BytesIO(data))
        document = PDFDocument(parser)
        pages = 0
        for _page in PDFPage.create_pages(document):
            pages += 1
            if pages > MAX_PDF_PAGES:
                raise CvRejected(
                    f"That PDF has more than {MAX_PDF_PAGES} pages, which is longer than any CV."
                )
    except (PDFPasswordIncorrect, PDFEncryptionError):
        raise CvRejected(
            "That PDF is password-protected. Remove the password and upload it again."
        ) from None
    except CvRejected:
        raise
    except (PDFSyntaxError, Exception):  # noqa: BLE001 - pdfminer raises a wide family
        raise CvRejected("That PDF is damaged and can't be opened.") from None
    if pages == 0:
        raise CvRejected("That PDF has no pages.")
    return pages


# --- content -----------------------------------------------------------------


def _readable_text(data: bytes, media_type: str) -> str:
    from apps.resumes.parsing.base import ExtractionFailed
    from apps.resumes.parsing.text import extract_text

    try:
        return extract_text(data, mime=media_type)
    except ExtractionFailed as error:
        raise CvRejected(
            str(error.args[0]) if error.args else "That file couldn't be read."
        ) from None


def _check_is_a_cv(text: str, media_type: str) -> None:
    from apps.resumes.parsing import extract

    parsed = extract.parse(text, mime=media_type)
    if extract.NO_TEXT_WARNING in parsed.warnings:
        raise CvRejected(
            "That file has no readable text. It looks like a scan or a photo of a CV; "
            "export the CV from the program you wrote it in instead."
        )
    if extract.EMPTY_WARNING in parsed.warnings and len(text.strip()) < 40:
        raise CvRejected("That file has almost nothing in it.")
    known = {key for key in parsed.sections if key in extract.HEADINGS}
    if known or parsed.experience or parsed.education:
        return
    if len(_YEAR_RANGE.findall(text)) >= 2 and (parsed.emails or parsed.phones):
        # Dated roles and a way to reach the person, under headings this reader
        # does not know: still a CV.
        return
    raise CvRejected(
        "That file doesn't look like a CV: it has no sections such as Experience, "
        "Education or Skills, and no dated jobs or studies."
    )
