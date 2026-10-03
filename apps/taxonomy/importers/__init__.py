"""
Taxonomy import (SRS §5.3).

The curated seed is a working core; the real taxonomy arrives from outside —
ESCO today, O*NET or a national classification later. Nothing in here knows
which: a source produces `SkillRecord`s, the runner writes them, and the two
never meet. Adding a source is one class and one registry line.

    from apps.taxonomy.importers import CsvSource, run_import

    run_import(CsvSource(path="esco_skills.csv", source="esco"))
"""

from .base import SkillRecord, TaxonomySource
from .csv_source import CsvSource
from .registry import available_formats, get_format, register_format
from .runner import ImportSummary, run_import

__all__ = [
    "CsvSource",
    "ImportSummary",
    "SkillRecord",
    "TaxonomySource",
    "available_formats",
    "get_format",
    "register_format",
    "run_import",
]
