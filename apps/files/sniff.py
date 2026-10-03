"""
Content-based file type detection (DEVELOPER.md §6.4).

A browser-supplied content type and a filename extension are both attacker
controlled. The only trustworthy signal is the bytes.

This is deliberately a small hand-written signature reader rather than a
dependency. `python-magic` needs libmagic, which is awkward on Windows
developer machines; `filetype` is pure Python but pulls in a signature database
for hundreds of formats this platform will never accept. The platform accepts
handful of formats, and a handful of signatures is a page of code. A dependency that is larger
than the code it replaces is not a saving.

DOCX needs more than a magic number: it is a ZIP container, and so is every JAR,
XLSX and ODT. The container is opened and its parts inspected, which is also
what makes a renamed archive fail here instead of inside a parser later.
"""

from __future__ import annotations

import zipfile
from typing import IO

from .constants import DOC, DOCX, JPEG, PDF, PNG

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_HEADER_BYTES = 8

#: Parts that must be present for a ZIP container to be a Word document.
_DOCX_REQUIRED_PARTS = ("[Content_Types].xml", "word/document.xml")


def detect(stream: IO[bytes]) -> str | None:
    """Return the detected media type, or None if it is not one we accept.

    Reads from the current position and restores it, so the caller can go on to
    hash and store the same stream.
    """
    position = stream.tell() if stream.seekable() else None
    header = stream.read(_HEADER_BYTES)
    if position is not None:
        stream.seek(position)

    if header.startswith(_PDF_MAGIC):
        return PDF
    if header.startswith(_PNG_MAGIC):
        return PNG
    if header.startswith(_JPEG_MAGIC):
        return JPEG
    if header.startswith(_ZIP_MAGIC):
        return _detect_zip_container(stream, position)
    if header.startswith(_OLE_MAGIC):
        return DOC if classify(stream) == "doc" else None
    return None


def _detect_zip_container(stream: IO[bytes], position: int | None) -> str | None:
    try:
        with zipfile.ZipFile(stream) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    finally:
        if position is not None and stream.seekable():
            stream.seek(position)

    if all(part in names for part in _DOCX_REQUIRED_PARTS):
        # A macro-enabled document or a template renamed to .docx is not a
        # Word document in the sense anybody means; `classify` says which.
        if classify(stream) != "docx":
            return None
        return DOCX
    return None


def extension_matches(filename: str, media_type: str) -> bool:
    """True when the filename's extension is consistent with the real type.

    A mismatch is not automatically an attack — people rename files — but it is
    worth refusing, because every downstream tool routes on extension and a
    disagreement between name and content is exactly what those tools mishandle.
    """
    from .constants import EXTENSIONS

    expected = EXTENSIONS.get(media_type)
    if expected is None:
        return False
    suffix = filename.rsplit(".", 1)
    if len(suffix) != 2:
        return False
    actual = f".{suffix[1].lower()}"
    if media_type == JPEG:
        return actual in {".jpg", ".jpeg"}
    return actual == expected


# --- classification -------------------------------------------------------
#
# `detect` answers "is this something we accept, and as what". When the answer
# is no, the person deserves to know *what* we think they sent — "that is a
# spreadsheet" is something they can act on, "that type is not accepted" is
# not. `classify` names the format from the bytes, for that message only; it
# never widens what `detect` accepts.

_GIF_MAGIC = (b"GIF87a", b"GIF89a")
_EXE_MAGIC = (b"MZ", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe")
_ARCHIVE_MAGIC = (b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c", b"\x1f\x8b", b"BZh")


def classify(stream: IO[bytes]) -> str:
    """A short name for what the bytes are: pdf, docx, doc, image, zip, ..."""
    position = stream.tell() if stream.seekable() else None
    head = stream.read(4096)
    if position is not None:
        stream.seek(position)

    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if (
        head.startswith((_PNG_MAGIC, _JPEG_MAGIC, *_GIF_MAGIC))
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        or head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1")
        or head.startswith((b"II*\x00", b"MM\x00*", b"BM"))
    ):
        return "image"
    if head.startswith(_ZIP_MAGIC):
        return _classify_zip(stream, position)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _classify_ole(stream, position)
    if head.startswith(_EXE_MAGIC):
        return "program"
    if head.startswith(_ARCHIVE_MAGIC):
        return "archive"
    if head.lstrip().startswith(b"{\rtf"):
        return "rtf"

    text = head.lstrip(b"\xef\xbb\xbf").lstrip().lower()
    if text.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<html" in text[:512]:
        return "html"
    if text.startswith(b"<svg") or b"<svg" in text[:512]:
        return "svg"
    if text.startswith(b"<?xml") or text.startswith(b"<"):
        return "xml"
    if _looks_like_text(head):
        return "csv" if _looks_like_csv(head) else "text"
    return "unknown"


def _looks_like_csv(head: bytes) -> bool:
    """Two or more lines with the same, non-zero count of one delimiter."""
    lines = [line for line in head.replace(b"\r", b"").split(b"\n")[:6] if line.strip()]
    if len(lines) < 2:
        return False
    # The last line of a 4 KB window may be cut short; judge the whole ones.
    whole = lines[:-1] if len(lines) > 2 else lines
    for delimiter in (b",", b";", b"\t"):
        counts = {line.count(delimiter) for line in whole}
        if len(counts) == 1 and counts.pop() >= 1:
            return True
    return False


def _looks_like_text(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _classify_zip(stream: IO[bytes], position: int | None) -> str:
    try:
        with zipfile.ZipFile(stream) as archive:
            names = set(archive.namelist())
            types = (
                archive.read("[Content_Types].xml")[:200_000].decode("utf-8", "replace")
                if "[Content_Types].xml" in names
                else ""
            )
    except (zipfile.BadZipFile, OSError, ValueError, KeyError, RuntimeError):
        return "zip"
    finally:
        if position is not None and stream.seekable():
            stream.seek(position)

    if "word/document.xml" in names:
        if "macroEnabled" in types or "word/vbaProject.bin" in names:
            return "docm"
        if "wordprocessingml.template" in types:
            return "dotx"
        return "docx"
    if any(n.startswith("xl/") for n in names):
        return "spreadsheet"
    if any(n.startswith("ppt/") for n in names):
        return "presentation"
    if "mimetype" in names or "content.xml" in names:
        return "opendocument"
    return "zip"


def _classify_ole(stream: IO[bytes], position: int | None) -> str:
    from .ole import NotCompoundFile, open_compound

    try:
        data = stream.read()
    finally:
        if position is not None and stream.seekable():
            stream.seek(position)
    try:
        compound = open_compound(data)
    except NotCompoundFile:
        return "unknown"
    streams = {name.lower() for name in compound.streams()}
    if "worddocument" in streams:
        return "doc"
    if "workbook" in streams or "book" in streams:
        return "spreadsheet"
    if "powerpoint document" in streams:
        return "presentation"
    if "encryptedpackage" in streams:
        return "encrypted-office"
    return "unknown"
