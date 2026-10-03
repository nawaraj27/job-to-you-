"""A deterministic transcriber for tests. Answers from a queue."""

from __future__ import annotations

import os
from typing import Any, ClassVar

from .base import SpeechFailed, SpeechUnavailable, Transcript, Word, tidy


class FakeTranscriber:
    key = "fake"

    #: Queued answers: a string (spoken at a steady pace), a Transcript, or an
    #: exception class to raise.
    queued: ClassVar[list[Any]] = []
    #: Paths that existed when they were read. Tests assert they are gone after.
    seen_paths: ClassVar[list[str]] = []

    def __init__(self, **_: Any) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        cls.queued = []
        cls.seen_paths = []

    @classmethod
    def queue(cls, *answers: Any) -> None:
        cls.queued.extend(answers)

    def transcribe(self, path: str) -> Transcript:
        if not os.path.exists(path):
            raise SpeechFailed("The recording was not there to read.")
        type(self).seen_paths.append(path)

        answer = type(self).queued.pop(0) if type(self).queued else "I did the work."
        if isinstance(answer, type) and issubclass(answer, (SpeechFailed, SpeechUnavailable)):
            raise answer("fake failure")
        if isinstance(answer, Transcript):
            return answer

        tokens = str(answer).split()
        words = [
            Word(text=token, start=0.5 + i * 0.4, end=0.5 + i * 0.4 + 0.3)
            for i, token in enumerate(tokens)
        ]
        return Transcript(
            text=tidy(tokens),
            words=words,
            duration_seconds=(words[-1].end + 0.5) if words else 1.0,
            engine="fake",
        )


class UnavailableTranscriber:
    """What an installation without a speech engine gets."""

    key = "unavailable"

    def __init__(self, **_: Any) -> None:
        pass

    def transcribe(self, path: str) -> Transcript:
        raise SpeechUnavailable("Speech recognition is not set up on this server.")
