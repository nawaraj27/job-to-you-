"""
The one definition of "the same words".

Lives apart from both models and services because both need it and neither may
import the other: `SkillAlias.save()` derives its match column from this, and
resolution matches against that column. If the two ever computed it differently,
aliases would silently resolve to nothing — the failure mode with no error
message.

Unicode-aware by construction, not by later retrofit. A whitelist of ASCII
characters would fold "लेखा" to the empty string, which does not fail loudly:
it makes every Nepali alias match nothing at all. So the rule is stated the
other way round — drop punctuation and symbols, keep everything else — and
combining marks, which carry the vowels in Devanagari, are kept because they are
neither.
"""

from __future__ import annotations

import re
import unicodedata

#: Punctuation that is part of a skill's identity rather than noise. C++ and C#
#: are mostly punctuation; folding it collapses three languages onto "c".
_SIGNIFICANT = frozenset("+#.")

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Fold case, punctuation and whitespace to the form aliases are stored in."""
    # NFKC first, so "ＪＳ" and "JS" are the same key and decomposed Devanagari
    # matches its composed spelling.
    folded = unicodedata.normalize("NFKC", text or "").strip().lower()

    characters = []
    for character in folded:
        if character in _SIGNIFICANT:
            characters.append(character)
        elif unicodedata.category(character)[0] in {"P", "S", "C", "Z"}:
            characters.append(" ")
        else:
            characters.append(character)

    return _WHITESPACE.sub(" ", "".join(characters)).strip()
