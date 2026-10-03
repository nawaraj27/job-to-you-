"""
Choosing a speech-to-text engine.

Settings, not a database row, for the reason the video provider and AI routing
are code: swapping an engine is a deploy.

    SPEECH_TO_TEXT_PROVIDER = "apps.interviews.speech.local.FasterWhisperTranscriber"
    SPEECH_TO_TEXT_OPTIONS = {"model": "base.en"}

With nothing configured, the local engine is used when it is installed and the
unavailable one otherwise — in which case the voice interview says so and the
candidate types their answers.
"""

from __future__ import annotations

import importlib.util

from django.conf import settings
from django.utils.module_loading import import_string

from .base import SpeechFailed, SpeechUnavailable, Transcriber, Transcript, Word, tidy

__all__ = [
    "SpeechFailed",
    "SpeechUnavailable",
    "Transcriber",
    "Transcript",
    "Word",
    "is_available",
    "tidy",
    "transcriber",
]

LOCAL = "apps.interviews.speech.local.FasterWhisperTranscriber"
UNAVAILABLE = "apps.interviews.speech.fake.UnavailableTranscriber"


def _default_path() -> str:
    return LOCAL if importlib.util.find_spec("faster_whisper") is not None else UNAVAILABLE


def transcriber() -> Transcriber:
    path = getattr(settings, "SPEECH_TO_TEXT_PROVIDER", "") or _default_path()
    options = getattr(settings, "SPEECH_TO_TEXT_OPTIONS", {})
    return import_string(path)(**options)


def is_available() -> bool:
    """Whether recording an answer can work on this installation."""
    path = getattr(settings, "SPEECH_TO_TEXT_PROVIDER", "") or _default_path()
    return path != UNAVAILABLE
