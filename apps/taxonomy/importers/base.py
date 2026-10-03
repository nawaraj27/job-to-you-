"""
What a taxonomy source has to provide, and nothing more.

The contract is deliberately narrow so that a source can be a CSV file, an API,
a SPARQL endpoint or a fixture without the runner changing. Two rules make
resumption possible, and both belong to the source:

  * every record carries a **stable** `external_id` — stable across exports, or
    a re-import creates duplicates instead of updating
  * records arrive in **ascending `external_id` order** — the runner stores the
    last id it committed, and "everything after this id" is only meaningful if
    the order is total and repeatable

`records()` must be re-iterable: the runner reads the source twice, once to
write skills and once to link hierarchy, because a parent can appear after its
child in any ordering keyed on id.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillRecord:
    """One skill as an external taxonomy describes it.

    Everything except `external_id` and `name` is optional, because sources
    differ wildly in what they carry and a required field nobody has forces
    importers to invent values.
    """

    external_id: str
    name: str
    description: str = ""
    category: str = ""
    #: A `SkillKind` value. Unknown by default: a wrong classification is worse
    #: than an unset one, because a curator can find "unknown" and cannot find
    #: "confidently mislabelled".
    kind: str = ""
    #: Surface forms. Claimed aliases are never stolen from a curated skill.
    aliases: tuple[str, ...] = ()
    #: locale -> display name. English belongs in `name`, not here.
    translations: Mapping[str, str] = field(default_factory=dict)
    #: External ids of skills broader than this one. Linked in a second pass.
    broader: tuple[str, ...] = ()
    uri: str = ""

    def __post_init__(self) -> None:
        if not self.external_id.strip():
            raise ValueError("A skill record needs a stable external id.")
        if not self.name.strip():
            raise ValueError(f"Record {self.external_id!r} has no name.")


class TaxonomySource(abc.ABC):
    """A place skills come from."""

    #: Recorded on every `SkillExternalId` and `ImportRun` this source produces.
    #: Two sources must not share one name, or their ids collide.
    source: str

    @abc.abstractmethod
    def records(self, *, after: str = "") -> Iterator[SkillRecord]:
        """Yield records in ascending `external_id` order.

        `after` is a resumption hint: a source that can push the skip down to
        the file or query should honour it. Sources that cannot may ignore it —
        the runner filters defensively either way, so correctness never depends
        on the source getting this right.
        """

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> TaxonomySource:
        """Build from command-line options. Overridden by sources with settings."""
        raise NotImplementedError(f"{cls.__name__} cannot be built from options.")

    def __str__(self) -> str:
        return self.source
