"""
Deterministic resume reading (SRS §5.5, P4).

Deterministic is the operative word: no model, no network, no AI call. The same
file parses to the same structure every time, which is what makes a parse
cacheable, testable and explainable — and it is what makes the P4 exit
criterion checkable, because "a guest upload cannot trigger a single AI call"
is only verifiable if the parser has no way to make one.

    text = extract_text(attachment)      # bytes -> text, per format
    parsed = parse(text)                 # text -> ParsedResume
"""

from .base import ENGINE, ENGINE_VERSION, ExtractionFailed, ParsedResume, UnsupportedFormat
from .extract import parse
from .text import extract_text, page_count

__all__ = [
    "ENGINE",
    "ENGINE_VERSION",
    "ExtractionFailed",
    "ParsedResume",
    "UnsupportedFormat",
    "extract_text",
    "page_count",
    "parse",
]
