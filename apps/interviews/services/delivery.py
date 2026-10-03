"""
Delivery signals from a spoken answer (voice practice).

**What this measures, and only this:** how long the candidate spoke, how fast,
how many long silences there were, how quickly they started, and how many filler
words the recogniser kept. Each is a count or a duration read off the word
timings. None of it is a judgement about the person.

**What it never does:** look at the camera. The video preview exists so a
candidate can rehearse being on camera; the frames never leave the browser, and
nothing here could read them if they did. Facial expression, eye movement,
posture and appearance do not predict honesty, competence or confidence, and a
feature that scored them would be measuring the wrong thing with confidence.

**What it is never used for:** scoring. Delivery feedback is shown to the
candidate beside their practice result. It is not given to the evaluator, not
averaged into a competency, and not shown to an employer — the same rule as
`integrity_meta`, for the same reason.

The thresholds are deliberately wide and the wording is a suggestion, not a
verdict: conversational English is commonly cited at roughly 120-160 words per
minute, nerves change pace, and an accent or a second language changes both
pace and filler words for reasons that have nothing to do with the answer.
"""

from __future__ import annotations

from typing import Any

from django.core import signing

from ..speech import Transcript

#: A silence at least this long between two words counts as a long pause.
LONG_PAUSE_SECONDS = 2.0

#: Below this many words the pace figure is noise, and is not reported.
MIN_WORDS_FOR_PACE = 20

SLOW_WPM = 100
FAST_WPM = 175
SHORT_ANSWER_WORDS = 40

#: Fillers the recogniser keeps. Whisper models tend to drop disfluencies, so
#: this undercounts, and the feedback says so.
FILLERS = frozenset({"um", "uh", "er", "erm", "hmm", "ah"})

SIGNING_SALT = "interviews.delivery"

#: The keys an answer may carry. Allow-listed like `integrity_meta`.
KEYS = (
    "engine",
    "recorded_seconds",
    "speaking_seconds",
    "words",
    "words_per_minute",
    "long_pauses",
    "longest_pause_seconds",
    "start_delay_seconds",
    "fillers",
)


def signals(transcript: Transcript) -> dict[str, Any]:
    """The measurements, from the word timings."""
    words = transcript.words
    count = len(words)
    speaking = (words[-1].end - words[0].start) if count else 0.0

    gaps = [later.start - earlier.end for earlier, later in zip(words, words[1:], strict=False)]
    long_gaps = [gap for gap in gaps if gap >= LONG_PAUSE_SECONDS]
    fillers = sum(1 for word in words if word.text.strip(" .,!?").lower() in FILLERS)

    return {
        "engine": transcript.engine[:40],
        "recorded_seconds": round(transcript.duration_seconds, 1),
        "speaking_seconds": round(speaking, 1),
        "words": count,
        "words_per_minute": round(count / (speaking / 60))
        if speaking > 0 and count >= MIN_WORDS_FOR_PACE
        else None,
        "long_pauses": len(long_gaps),
        "longest_pause_seconds": round(max(long_gaps), 1) if long_gaps else 0.0,
        "start_delay_seconds": round(words[0].start, 1) if count else None,
        "fillers": fillers,
    }


def sign(measured: dict[str, Any]) -> str:
    """Seal the measurements so the browser can carry them to the submit.

    The transcript is the candidate's to edit; the timings are not — they were
    measured on the server, and a submitted answer should not be able to claim a
    pace nobody spoke at.
    """
    return signing.dumps(clean(measured), salt=SIGNING_SALT, compress=True)


def unsign(token: str, *, max_age_seconds: int = 60 * 60) -> dict[str, Any]:
    """The sealed measurements, or `{}` when missing, altered or stale."""
    if not token:
        return {}
    try:
        return clean(signing.loads(token, salt=SIGNING_SALT, max_age=max_age_seconds))
    except signing.BadSignature:
        return {}


def clean(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in KEYS:
        value = source.get(key)
        if key == "engine":
            out[key] = str(value or "")[:40]
        elif value is None:
            out[key] = None
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                out[key] = None
                continue
            out[key] = max(number, 0) if key.endswith("seconds") else max(int(number), 0)
    return out


def feedback(measured: dict[str, Any]) -> list[str]:
    """Plain suggestions, each tied to the number it comes from."""
    notes: list[str] = []
    if not measured:
        return notes

    words = measured.get("words") or 0
    pace = measured.get("words_per_minute")
    speaking = measured.get("speaking_seconds") or 0

    if words and words < SHORT_ANSWER_WORDS:
        notes.append(
            f"This answer was {words} words ({speaking:.0f} seconds of speech). Answers that "
            "cover the situation, what you did and what happened usually take one to two minutes."
        )
    if pace is not None and pace > FAST_WPM:
        notes.append(
            f"You spoke at about {pace} words a minute. Slowing down a little gives the "
            "interviewer time to follow the detail."
        )
    elif pace is not None and pace < SLOW_WPM:
        notes.append(
            f"You spoke at about {pace} words a minute. That can be fine; if you were searching "
            "for words, sketching the situation, action and result before starting helps."
        )
    if (measured.get("long_pauses") or 0) >= 3:
        notes.append(
            f"There were {measured['long_pauses']} silences of two seconds or more (the longest "
            f"{measured.get('longest_pause_seconds', 0):.0f} seconds)."
        )
    if (measured.get("fillers") or 0) >= 5:
        notes.append(
            f'The recogniser heard {measured["fillers"]} filler words such as "um". It tends to '
            "miss them, so the real number is probably higher."
        )
    return notes


def summarise(turns: list[Any]) -> dict[str, Any]:
    """Across a whole voice session, for the result page."""
    measured = [turn.delivery_meta for turn in turns if getattr(turn, "delivery_meta", None)]
    if not measured:
        return {}
    paces = [m["words_per_minute"] for m in measured if m.get("words_per_minute")]
    return {
        "answers": len(measured),
        "speaking_seconds": round(sum(m.get("speaking_seconds") or 0 for m in measured)),
        "median_words_per_minute": sorted(paces)[len(paces) // 2] if paces else None,
        "long_pauses": sum(m.get("long_pauses") or 0 for m in measured),
        "fillers": sum(m.get("fillers") or 0 for m in measured),
    }
