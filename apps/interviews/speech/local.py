"""
Local transcription with faster-whisper (CTranslate2 Whisper).

Chosen over a hosted speech API for the reason `embed.text` runs locally: the
volume follows how much candidates practise, per-minute pricing compounds into a
bill nobody decided on, and a candidate's voice never leaves the server.

Measured on the development machine (CPU, int8, `base.en`), on the synthetic
corpus in `speech_benchmark.json`: 4.8% word error rate over 290 words, about
1.1 seconds to transcribe a 10-24 second answer. Model weights (~145 MB) are
downloaded once on first use and cached by the library.

The model is loaded once per process and shared: loading takes seconds, and a
second copy per request would exhaust memory under any real load.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .base import SpeechFailed, SpeechUnavailable, Transcript, Word, tidy

logger = logging.getLogger(__name__)

_models: dict[tuple[str, str, str], Any] = {}
_lock = threading.Lock()


class FasterWhisperTranscriber:
    key = "faster-whisper"

    def __init__(
        self,
        *,
        model: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language

    def _model(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise SpeechUnavailable("faster-whisper is not installed.") from exc

        key = (self.model_name, self.device, self.compute_type)
        with _lock:
            if key not in _models:
                _models[key] = WhisperModel(
                    self.model_name, device=self.device, compute_type=self.compute_type
                )
            return _models[key]

    def transcribe(self, path: str) -> Transcript:
        model = self._model()
        try:
            segments, info = model.transcribe(
                path,
                language=self.language,
                word_timestamps=True,
                # Voice activity detection off: it trims silences, and the pauses
                # are part of what the delivery feedback reports.
                vad_filter=False,
                beam_size=5,
            )
            words = [
                Word(text=word.word, start=float(word.start), end=float(word.end))
                for segment in segments
                if not is_hallucinated_silence(segment)
                for word in (segment.words or [])
            ]
        except Exception as exc:  # noqa: BLE001 - decoding failures are many and opaque
            logger.info("transcription failed", exc_info=True)
            raise SpeechFailed("The recording could not be read.") from exc

        return Transcript(
            text=tidy([word.text for word in words]),
            words=words,
            duration_seconds=float(getattr(info, "duration", 0.0) or 0.0),
            engine=f"{self.key}:{self.model_name}",
        )


#: Whisper writes words into silence. Measured on a six-second silent recording
#: from the browser: one segment reading "You", no-speech probability 0.62. Real
#: speech in the corpus scored 0.00. Without this the page showed a one-word
#: "transcript" instead of saying nothing was heard.
NO_SPEECH_PROBABILITY = 0.5


def is_hallucinated_silence(segment: Any) -> bool:
    return float(getattr(segment, "no_speech_prob", 0.0) or 0.0) >= NO_SPEECH_PROBABILITY
