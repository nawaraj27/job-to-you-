"""
A skill source backed by a delimited file.

CSV is the shape every taxonomy export reduces to, so this one source covers
ESCO, O*NET and whatever a ministry emails over, without any of them being named
in the code. Header names are matched against a small set of accepted spellings
(`external_id`, `conceptUri`, `onetsoc_code`, ...) so real exports load unedited.

Multi-valued cells are pipe-separated, because commas are already the delimiter
and skill labels contain them:

    aliases        Postgres|psql
    translations   ne=लेखा|hi=लेखांकन
    broader        http://data.europa.eu/esco/skill/1234
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .base import SkillRecord, TaxonomySource
from .registry import register_format

#: field -> header spellings accepted for it, in priority order.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "external_id": ("external_id", "concept_uri", "concepturi", "uri", "id", "onetsoc_code"),
    "name": ("name", "preferred_label", "preferredlabel", "title", "label"),
    "description": ("description", "definition", "scope_note"),
    "category": ("category", "skill_type", "group", "domain"),
    "kind": ("kind", "skill_kind", "type"),
    "aliases": ("aliases", "alt_labels", "altlabels", "synonyms"),
    "translations": ("translations", "localised_names"),
    "broader": ("broader", "broader_uri", "parent", "parent_id"),
    "uri": ("uri", "concept_uri", "concepturi", "url"),
}

MULTI_VALUE_SEPARATOR = "|"


@register_format("csv")
class CsvSource(TaxonomySource):
    def __init__(
        self,
        *,
        path: str | Path,
        source: str,
        delimiter: str = ",",
        encoding: str = "utf-8-sig",
        sort_in_memory: bool = True,
    ) -> None:
        self.path = Path(path)
        self.source = source
        self.delimiter = delimiter
        self.encoding = encoding
        # Resumption needs a total order on external_id. Most exports are not
        # sorted, so by default the file is ordered here — a few tens of
        # thousands of rows is nothing. Pass False for a genuinely large,
        # already-sorted file and the read stays constant-memory.
        self.sort_in_memory = sort_in_memory

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> CsvSource:
        path = options.get("path")
        if not path:
            raise ValueError("The csv format needs --path.")
        return cls(
            path=path,
            source=options.get("source") or "csv",
            delimiter=options.get("delimiter") or ",",
            sort_in_memory=not options.get("presorted", False),
        )

    def records(self, *, after: str = "") -> Iterator[SkillRecord]:
        import csv

        if not self.path.exists():
            raise ValueError(f"No such file: {self.path}")

        with self.path.open(encoding=self.encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=self.delimiter)
            headers = [h.strip().lower() for h in (reader.fieldnames or [])]
            mapping = self._resolve_columns(headers)

            rows: Iterator[dict[str, str]] = (
                {(k or "").strip().lower(): (v or "") for k, v in row.items()} for row in reader
            )
            if self.sort_in_memory:
                rows = iter(sorted(rows, key=lambda r: r.get(mapping["external_id"], "")))

            for row in rows:
                record = self._to_record(row, mapping)
                if record is None or record.external_id <= after:
                    continue
                yield record

    def _resolve_columns(self, headers: list[str]) -> dict[str, str]:
        """Map our field names onto this file's actual headers."""
        mapping: dict[str, str] = {}
        for field_name, candidates in COLUMN_ALIASES.items():
            for candidate in candidates:
                if candidate in headers:
                    mapping[field_name] = candidate
                    break

        missing = {"external_id", "name"} - set(mapping)
        if missing:
            raise ValueError(
                f"{self.path.name} has no column for {sorted(missing)}. "
                f"Headers found: {headers}. Accepted spellings: "
                f"{ {k: COLUMN_ALIASES[k] for k in sorted(missing)} }"
            )
        return mapping

    def _to_record(self, row: Mapping[str, str], mapping: Mapping[str, str]) -> SkillRecord | None:
        def cell(field_name: str) -> str:
            column = mapping.get(field_name)
            return (row.get(column, "") if column else "").strip()

        external_id, name = cell("external_id"), cell("name")
        if not external_id or not name:
            # A row without an identity or a label is not a skill. Skipped
            # rather than fatal: one malformed line should not void an import.
            return None

        return SkillRecord(
            external_id=external_id,
            name=name,
            description=cell("description"),
            category=cell("category"),
            kind=cell("kind"),
            aliases=_split(cell("aliases")),
            translations=_parse_translations(cell("translations")),
            broader=_split(cell("broader")),
            uri=cell("uri"),
        )


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(MULTI_VALUE_SEPARATOR) if part.strip())


def _parse_translations(value: str) -> dict[str, str]:
    """`ne=लेखा|hi=लेखांकन` -> {"ne": "लेखा", "hi": "लेखांकन"}."""
    out: dict[str, str] = {}
    for part in _split(value):
        locale, separator, name = part.partition("=")
        if separator and locale.strip() and name.strip():
            out[locale.strip()] = name.strip()
    return out
