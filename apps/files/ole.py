"""
A read-only reader for OLE2 compound files (MS-CFB), enough to open a Word
97-2003 `.doc`.

A `.doc` is a small FAT file system inside one file: fixed-size sectors, a
sector allocation table, a directory of named streams, and a "mini stream" for
streams under 4 KB. Word keeps the document in the `WordDocument` stream and
its piece table in `0Table` or `1Table`.

Written here rather than taken as a dependency for the same reason `sniff.py`
is: the platform needs to list streams and read three of them, which is a page
of code, and every line of it treats the file as hostile. Chains are bounded by
the sector count (a cycle cannot loop forever), every offset is range-checked,
and nothing in the file is executed — macros live in their own stream and are
never read.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_FREE = 0xFFFFFFFF
_END_OF_CHAIN = 0xFFFFFFFE
_FAT_SECTOR = 0xFFFFFFFD
_DIFAT_SECTOR = 0xFFFFFFFC
_SPECIAL = {_FREE, _END_OF_CHAIN, _FAT_SECTOR, _DIFAT_SECTOR}

_STREAM = 2
_ROOT = 5


class NotCompoundFile(ValueError):
    """The bytes are not a readable compound file."""


@dataclass(frozen=True)
class _Entry:
    name: str
    kind: int
    start: int
    size: int


class CompoundFile:
    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(MAGIC):
            raise NotCompoundFile("not a compound file")
        self._data = data
        (
            major,
            byte_order,
            sector_shift,
            mini_shift,
        ) = struct.unpack_from("<HHHH", data, 0x1A)
        if byte_order != 0xFFFE or major not in (3, 4) or sector_shift not in (9, 12):
            raise NotCompoundFile("unsupported compound file header")
        self._sector_size = 1 << sector_shift
        self._mini_size = 1 << mini_shift
        (
            fat_sectors,
            first_dir,
            _transaction,
            self._mini_cutoff,
            first_mini_fat,
            mini_fat_sectors,
            first_difat,
            difat_sectors,
        ) = struct.unpack_from("<IIIIIIII", data, 0x2C)
        self._sector_count = max(0, (len(data) - self._sector_size) // self._sector_size)

        fat_locations = [n for n in struct.unpack_from("<109I", data, 0x4C) if n not in _SPECIAL]
        sector = first_difat
        for _ in range(min(difat_sectors, self._sector_count)):
            if sector in _SPECIAL:
                break
            block = self._sector(sector)
            entries = struct.unpack_from(f"<{self._sector_size // 4}I", block)
            fat_locations.extend(n for n in entries[:-1] if n not in _SPECIAL)
            sector = entries[-1]
        fat_locations = fat_locations[:fat_sectors]

        fat: list[int] = []
        for location in fat_locations:
            fat.extend(struct.unpack_from(f"<{self._sector_size // 4}I", self._sector(location)))
        self._fat = fat

        directory = self._chain_bytes(first_dir, self._fat, self._sector_size, self._sector)
        self._entries: list[_Entry] = []
        for offset in range(0, len(directory) - 127, 128):
            raw = directory[offset : offset + 128]
            name_length = struct.unpack_from("<H", raw, 64)[0]
            kind = raw[66]
            if kind not in (1, _STREAM, _ROOT) or not 2 <= name_length <= 64:
                continue
            name = raw[: name_length - 2].decode("utf-16-le", errors="replace")
            start = struct.unpack_from("<I", raw, 116)[0]
            size = struct.unpack_from("<Q", raw, 120)[0]
            if major == 3:
                size &= 0xFFFFFFFF
            self._entries.append(_Entry(name=name, kind=kind, start=start, size=size))

        root = next((e for e in self._entries if e.kind == _ROOT), None)
        if root is None:
            raise NotCompoundFile("compound file has no root entry")
        self._mini_stream = (
            self._chain_bytes(root.start, self._fat, self._sector_size, self._sector)[: root.size]
            if root.start not in _SPECIAL
            else b""
        )
        mini_fat_raw = (
            self._chain_bytes(first_mini_fat, self._fat, self._sector_size, self._sector)
            if mini_fat_sectors and first_mini_fat not in _SPECIAL
            else b""
        )
        self._mini_fat = list(struct.unpack_from(f"<{len(mini_fat_raw) // 4}I", mini_fat_raw))

    # --- public --------------------------------------------------------------

    def streams(self) -> set[str]:
        return {e.name for e in self._entries if e.kind == _STREAM}

    def entry_names(self) -> set[str]:
        """Streams and storages (directories) alike."""
        return {e.name for e in self._entries}

    def has_stream(self, name: str) -> bool:
        return any(e.kind == _STREAM and e.name.lower() == name.lower() for e in self._entries)

    def read(self, name: str, *, limit: int = 32 * 1024 * 1024) -> bytes:
        entry = next(
            (e for e in self._entries if e.kind == _STREAM and e.name.lower() == name.lower()),
            None,
        )
        if entry is None:
            raise KeyError(name)
        if entry.size > limit:
            raise NotCompoundFile(f"stream {name} is implausibly large")
        if entry.size < self._mini_cutoff:
            data = self._chain_bytes(
                entry.start, self._mini_fat, self._mini_size, self._mini_sector
            )
        else:
            data = self._chain_bytes(entry.start, self._fat, self._sector_size, self._sector)
        if len(data) < entry.size:
            raise NotCompoundFile(f"stream {name} is truncated")
        return data[: entry.size]

    # --- internals -----------------------------------------------------------

    def _sector(self, number: int) -> bytes:
        offset = (number + 1) * self._sector_size
        if number in _SPECIAL or offset + self._sector_size > len(self._data):
            raise NotCompoundFile("sector out of range")
        return self._data[offset : offset + self._sector_size]

    def _mini_sector(self, number: int) -> bytes:
        offset = number * self._mini_size
        if offset + self._mini_size > len(self._mini_stream):
            raise NotCompoundFile("mini sector out of range")
        return self._mini_stream[offset : offset + self._mini_size]

    @staticmethod
    def _chain_bytes(start: int, table: list[int], size: int, read) -> bytes:
        parts: list[bytes] = []
        sector = start
        seen = 0
        while sector not in _SPECIAL:
            if sector >= len(table) or seen > len(table):
                raise NotCompoundFile("broken sector chain")
            parts.append(read(sector))
            sector = table[sector]
            seen += 1
        return b"".join(parts)


def open_compound(data: bytes) -> CompoundFile:
    try:
        return CompoundFile(data)
    except (struct.error, IndexError) as error:
        raise NotCompoundFile("damaged compound file") from error
