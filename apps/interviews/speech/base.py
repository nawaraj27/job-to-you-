"""
The speech-to-text seam.

Same shape as the video-room and AI provider seams: one protocol, a local
implementation, a fake for tests, and nothing outside this package knows which
engine is in use.

**Audio is never stored.** A transcriber receives a path to a temporary file the
view deletes in a `finally`, and returns text and word timings. Nothing here
writes the audio anywhere else, and the fake asserts the file exists only while
it is being read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


class SpeechUnavailable(Exception):
    """No speech engine is installed or configured. The candidate types instead."""


class SpeechFailed(Exception):
    """The engine ran and could not produce a transcript (unreadable audio, timeout)."""


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Transcript:
    text: str
    words: list[Word] = field(default_factory=list)
    duration_seconds: float = 0.0
    engine: str = ""


class Transcriber(Protocol):
    key: str

    def transcribe(self, path: str) -> Transcript:
        """Transcribe one audio file. Raises SpeechUnavailable or SpeechFailed."""
        ...


def tidy(words: list[str]) -> str:
    """Join recognised words into readable text.

    The engine attaches punctuation to the next token, so a plain join produced
    "the migration .actually, let me correct that .MyManager led it" — which the
    candidate then has to fix by hand before submitting.
    """
    text = " ".join(word.strip() for word in words if word.strip())
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"([.,!?;:])(?=[A-Za-z])", r"\1 ", text)
    return re.sub(r"\s{2,}", " ", text).strip()
