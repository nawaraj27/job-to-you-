"""
Text from a Word 97-2003 `.doc` (MS-DOC), read from its piece table.

A `.doc` does not store its text in one run. The `WordDocument` stream holds
the characters, and a *piece table* in the `0Table` or `1Table` stream says
which byte ranges, in which order and in which encoding (8-bit Windows-1252 or
UTF-16), make up the document. Reading the stream front to back gives text in
the order it was typed and edited, not the order it reads, so the piece table
is followed instead — the same thing Word does.

Only the main document is read (`ccpText` characters): headers, footnotes and
comments come after it and are not the CV. Fields keep their result and lose
their instruction, so a hyperlink reads as its text rather than as
`HYPERLINK "mailto:..."`.

Pure Python over `apps.files.ole`; no external converter, nothing executed.
"""

from __future__ import annotations

import struct

from apps.files.ole import NotCompoundFile, open_compound

from .base import ExtractionFailed

_WORD_IDENT = 0xA5EC
_F_COMPLEX = 0x0004
_F_ENCRYPTED = 0x0100
_F_WHICH_TABLE = 0x0200
#: Index of fcClx/lcbClx in FibRgFcLcb97.
_CLX_PAIR = 33
#: Index of ccpText in FibRgLw97.
_CCP_TEXT = 3


class EncryptedDocument(ExtractionFailed):
    pass


def is_encrypted(compound) -> bool:
    """Whether the Word file is password-protected (FIB `fEncrypted`)."""
    word = compound.read("WordDocument", limit=64 * 1024 * 1024)
    if len(word) < 12 or struct.unpack_from("<H", word, 0)[0] != _WORD_IDENT:
        raise ValueError("not a Word 97-2003 document")
    return bool(struct.unpack_from("<H", word, 0x0A)[0] & _F_ENCRYPTED)


def extract(data: bytes) -> str:
    try:
        compound = open_compound(data)
    except NotCompoundFile as error:
        raise ExtractionFailed("That Word file could not be read. It may be damaged.") from error
    if not compound.has_stream("WordDocument"):
        raise ExtractionFailed("That file is not a Word document.")

    try:
        word = compound.read("WordDocument")
        ident, _nfib = struct.unpack_from("<HH", word, 0)
        if ident != _WORD_IDENT:
            raise ExtractionFailed("That Word file could not be read. It may be damaged.")
        flags = struct.unpack_from("<H", word, 0x0A)[0]
        if flags & _F_ENCRYPTED:
            raise EncryptedDocument(
                "That Word file is password-protected. Remove the password and upload it again."
            )

        csw = struct.unpack_from("<H", word, 32)[0]
        lw_start = 34 + csw * 2
        cslw = struct.unpack_from("<H", word, lw_start)[0]
        ccp_text = struct.unpack_from("<I", word, lw_start + 2 + _CCP_TEXT * 4)[0]
        fc_start = lw_start + 2 + cslw * 4
        pairs = struct.unpack_from("<H", word, fc_start)[0]
        if pairs <= _CLX_PAIR:
            raise ExtractionFailed("That Word file is too old to read. Save it as DOCX or PDF.")
        fc_clx, lcb_clx = struct.unpack_from("<II", word, fc_start + 2 + _CLX_PAIR * 8)

        table_name = "1Table" if flags & _F_WHICH_TABLE else "0Table"
        table = compound.read(table_name)
    except (KeyError, struct.error, NotCompoundFile) as error:
        raise ExtractionFailed("That Word file could not be read. It may be damaged.") from error

    pieces = _pieces(table, fc_clx, lcb_clx)
    text = _assemble(word, pieces, limit=ccp_text)
    return clean(text)


def _pieces(table: bytes, fc_clx: int, lcb_clx: int) -> list[tuple[int, int, int, bool]]:
    """(cp_start, cp_end, byte_offset, compressed) for each piece, in order."""
    clx = table[fc_clx : fc_clx + lcb_clx]
    if len(clx) != lcb_clx or not clx:
        raise ExtractionFailed("That Word file could not be read. It may be damaged.")
    position = 0
    # Skip any property modifiers (Prc, clxt 0x01) before the piece table.
    while position < len(clx) and clx[position] == 0x01:
        size = struct.unpack_from("<h", clx, position + 1)[0]
        position += 3 + max(0, size)
    if position >= len(clx) or clx[position] != 0x02:
        raise ExtractionFailed("That Word file could not be read. It may be damaged.")
    lcb = struct.unpack_from("<I", clx, position + 1)[0]
    plc = clx[position + 5 : position + 5 + lcb]
    count = (len(plc) - 4) // 12
    if count <= 0:
        raise ExtractionFailed("That Word file has no text.")
    cps = struct.unpack_from(f"<{count + 1}I", plc, 0)
    pieces = []
    for index in range(count):
        descriptor = plc[(count + 1) * 4 + index * 8 : (count + 1) * 4 + (index + 1) * 8]
        fc = struct.unpack_from("<I", descriptor, 2)[0]
        compressed = bool(fc & 0x40000000)
        offset = (fc & 0x3FFFFFFF) // 2 if compressed else fc & 0x3FFFFFFF
        pieces.append((cps[index], cps[index + 1], offset, compressed))
    return pieces


def _assemble(word: bytes, pieces: list[tuple[int, int, int, bool]], *, limit: int) -> str:
    out: list[str] = []
    for cp_start, cp_end, offset, compressed in pieces:
        if cp_start >= limit:
            break
        cp_end = min(cp_end, limit)
        length = max(0, cp_end - cp_start)
        if compressed:
            raw = word[offset : offset + length]
            out.append(raw.decode("cp1252", errors="replace"))
        else:
            raw = word[offset : offset + length * 2]
            out.append(raw.decode("utf-16-le", errors="replace"))
    return "".join(out)


def clean(text: str) -> str:
    """Word's control characters, turned into the text they stand for."""
    out: list[str] = []
    # Fields: \x13 instruction \x14 result \x15. Keep the result only. Fields
    # nest, so the depth of "inside an instruction" is tracked per level.
    stack: list[bool] = []  # True while in the instruction part of a field
    for character in text:
        if character == "\x13":
            stack.append(True)
            continue
        if character == "\x14":
            if stack:
                stack[-1] = False
            continue
        if character == "\x15":
            if stack:
                stack.pop()
            continue
        if any(stack):
            continue
        out.append(character)
    text = "".join(out)
    text = text.replace("\x07\x07", "\n").replace("\x07", " | ")
    text = text.replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
    text = text.replace("\x1e", "-").replace("\x1f", "").replace("\xa0", " ")
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    lines = [line.strip().rstrip("|").strip() for line in text.split("\n")]
    return "\n".join(lines)
