"""
Source formats, by name.

A registry rather than an if-chain in the management command, so a new source
is a class plus a decorator and nothing in the command changes. Registration is
explicit — no scanning the filesystem for plugins, which turns a typo into a
silently missing importer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import TaxonomySource

_FORMATS: dict[str, type[TaxonomySource]] = {}


def register_format(name: str):
    """Register a source class under a format name."""

    def decorate(cls: type[TaxonomySource]) -> type[TaxonomySource]:
        existing = _FORMATS.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"Import format {name!r} is already registered to {existing.__name__}."
            )
        _FORMATS[name] = cls
        return cls

    return decorate


def available_formats() -> list[str]:
    return sorted(_FORMATS)


def get_format(name: str, options: Mapping[str, Any]) -> TaxonomySource:
    try:
        cls = _FORMATS[name]
    except KeyError:
        raise ValueError(
            f"Unknown import format {name!r}. Available: {', '.join(available_formats()) or 'none'}."
        ) from None
    return cls.from_options(options)
