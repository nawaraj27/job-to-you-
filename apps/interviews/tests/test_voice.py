"""
Spoken practice interviews.

What is held here, from the failing side:

  * **audio is never stored** — the temporary file is gone when the request
    returns, and nothing lands in media or private storage;
  * the transcription endpoint refuses everybody and everything it should:
    another candidate, a text interview, a finished one, an answered question,
    no audio, too much audio, not audio, too many attempts, no CSRF token;
  * an unavailable or failing speech engine returns a sentence the page can
    show, with typing as the way forward;
  * delivery timings are measured on the server and sealed — an altered seal
    stores nothing;
  * **delivery never reaches the evaluator**, and the evaluator is told the
    answers were transcribed;
  * spoken interviews are practice only.

The speech engine is `FakeTranscriber`. Recognition accuracy on real audio is
measured by `manage.py interview_benchmark --only speech`, not here.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.ai.models import AIRun
from apps.ai.providers.fake import FakeAdapter
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.interviews.models import InterviewKind, InterviewSession, SessionState
from apps.interviews.services import delivery, engine, evaluation
from apps.interviews.speech import SpeechFailed, SpeechUnavailable, Transcript, Word
from apps.interviews.speech.fake import FakeTranscriber
from apps.interviews.tests.factories import fake_ai
from apps.interviews.tests.test_screens import ScreenFixture
from apps.matching.tests.test_matching import application_for

SPOKEN = (
    "I rebuilt the nightly reconciliation job, um, and cut it from four hours to thirty "
    "minutes by adding an index on the transaction reference and batching the writes."
)


def audio(size: int = 2048, content_type: str = "audio/webm") -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "answer.webm", b"\x1a\x45\xdf\xa3" + b"0" * size, content_type=content_type
    )


def stored_files() -> int:
    count = 0
    for root in (settings.MEDIA_ROOT, settings.PRIVATE_STORAGE_ROOT):
        for _, _, files in os.walk(root):
            count += len(files)
    return count


class VoiceFixture(ScreenFixture):
    def setUp(self):
        super().setUp()
        FakeTranscriber.reset()
        cache.clear()

    def voice_session(self) -> InterviewSession:
        with fake_ai():
            return engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                kind=InterviewKind.AI_VOICE,
            )

    def current_turn(self, session):
        return self.client.get(reverse("interviews:session", args=[session.public_id])).context[
            "turn"
        ]

    def transcribe(self, session, turn, **extra):
        data = {"turn": str(turn.pk), "audio": extra.pop("upload", audio())}
        return self.client.post(
            reverse("interviews:transcribe", args=[session.public_id]), data, **extra
        )


class StartTests(VoiceFixture):
    def test_the_hub_offers_answering_aloud_and_says_what_happens_to_the_audio(self):
        response = self.client.get(reverse("interviews:hub"))

        self.assertContains(response, 'value="voice"')
        self.assertContains(response, "recording is deleted")
        self.assertContains(response, "your face is not analysed")

    def test_choosing_voice_starts_a_spoken_practice(self):
        self.client.post(
            reverse("interviews:practice"), {"template": self.template.slug, "mode": "voice"}
        )

        session = InterviewSession.objects.get(candidate=self.profile)
        self.assertEqual(session.kind, InterviewKind.AI_VOICE)
        self.assertTrue(session.is_practice)

    def test_an_unfinished_typed_practice_is_not_resumed_as_a_spoken_one(self):
        url = reverse("interviews:practice")
        self.client.post(url, {"template": self.template.slug, "mode": "text"})
        self.client.post(url, {"template": self.template.slug, "mode": "voice"})

        kinds = sorted(
            InterviewSession.objects.filter(candidate=self.profile).values_list("kind", flat=True)
        )
        self.assertEqual(kinds, [InterviewKind.AI_TEXT, InterviewKind.AI_VOICE])

    def test_spoken_interviews_are_practice_only(self):
        application = application_for(self.profile, self.job)
        with fake_ai(), self.assertRaises(InvalidState):
            engine.start(
                candidate=self.profile,
                template=self.template,
                actor=self.profile.user,
                application=application,
                kind=InterviewKind.AI_VOICE,
            )


class ScreenTests(VoiceFixture):
    def test_the_spoken_screen_has_the_recorder_and_the_disclosure(self):
        session = self.voice_session()
        response = self.client.get(reverse("interviews:session", args=[session.public_id]))

        self.assertContains(response, reverse("interviews:transcribe", args=[session.public_id]))
        self.assertContains(response, "Video is never recorded or sent anywhere")
        self.assertContains(response, "Your browser blocked the microphone")
        self.assertContains(response, 'id="delivery-sealed"')
        # Typing is always there.
        self.assertContains(response, 'id="answer"')

    def test_a_typed_interview_has_no_recorder(self):
        session = self.practice_session()
        response = self.client.get(reverse("interviews:session", args=[session.public_id]))

        self.assertNotContains(response, "voiceAnswer")
        self.assertNotContains(response, 'id="delivery-sealed"')

    @override_settings(SPEECH_TO_TEXT_PROVIDER="apps.interviews.speech.fake.UnavailableTranscriber")
    def test_without_a_speech_engine_the_page_says_so(self):
        session = self.voice_session()
        response = self.client.get(reverse("interviews:session", args=[session.public_id]))

        # The recorder is told there is no engine, and opens in the state that
        # says so rather than offering a button that fails after recording.
        self.assertContains(response, ", false)")


class TranscribeTests(VoiceFixture):
    def test_a_recording_comes_back_as_text_with_sealed_timings(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        FakeTranscriber.queue(SPOKEN)

        response = self.transcribe(session, turn)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("reconciliation job", body["text"])
        self.assertEqual(body["measured"]["words"], len(SPOKEN.split()))
        self.assertEqual(body["measured"]["fillers"], 1)
        self.assertTrue(body["sealed"])

    def test_the_audio_is_deleted_before_the_response(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        before = stored_files()

        self.transcribe(session, turn)

        self.assertEqual(len(FakeTranscriber.seen_paths), 1)
        self.assertFalse(Path(FakeTranscriber.seen_paths[0]).exists())
        self.assertEqual(stored_files(), before)

    def test_the_audio_is_deleted_when_transcription_fails(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        FakeTranscriber.queue(SpeechFailed)

        response = self.transcribe(session, turn)

        self.assertEqual(response.status_code, 422)
        self.assertIn("type your answer", response.json()["error"])
        self.assertFalse(Path(FakeTranscriber.seen_paths[0]).exists())

    def test_no_speech_engine_says_to_type(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        FakeTranscriber.queue(SpeechUnavailable)

        response = self.transcribe(session, turn)

        self.assertEqual(response.status_code, 503)
        self.assertIn("Type your answer", response.json()["error"])

    def test_silence_is_not_an_answer(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        FakeTranscriber.queue(Transcript(text="", words=[], duration_seconds=5.0, engine="fake"))

        response = self.transcribe(session, turn)

        self.assertEqual(response.status_code, 422)
        self.assertIn("No speech was heard", response.json()["error"])

    def test_another_candidate_cannot_transcribe_into_this_interview(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        self.client.force_login(create_profile().user)

        self.assertEqual(self.transcribe(session, turn).status_code, 404)
        self.assertEqual(FakeTranscriber.seen_paths, [])

    def test_a_typed_interview_takes_no_recordings(self):
        session = self.practice_session()
        turn = self.current_turn(session)
        self.assertEqual(self.transcribe(session, turn).status_code, 409)

    def test_a_finished_interview_takes_no_recordings(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        engine.abandon(session=session)
        self.assertEqual(self.transcribe(session, turn).status_code, 409)

    def test_an_answered_question_takes_no_recordings(self):
        # 409 and marked stale, not 404: the question exists, it has simply
        # been answered (see test_question_replay).
        session = self.voice_session()
        turn = self.current_turn(session)
        engine.submit_answer(session=session, turn=turn, text="Typed instead.")
        self.assertEqual(self.transcribe(session, turn).status_code, 409)

    def test_no_audio_is_refused(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        response = self.client.post(
            reverse("interviews:transcribe", args=[session.public_id]), {"turn": str(turn.pk)}
        )
        self.assertEqual(response.status_code, 400)

    def test_a_recording_that_is_too_large_is_refused_unread(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        with mock.patch("portals.candidate.interview_views.MAX_AUDIO_BYTES", 1000):
            response = self.transcribe(session, turn)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(FakeTranscriber.seen_paths, [])

    def test_something_that_is_not_audio_is_refused_unread(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        response = self.transcribe(session, turn, upload=audio(content_type="text/html"))
        self.assertEqual(response.status_code, 415)
        self.assertEqual(FakeTranscriber.seen_paths, [])

    def test_attempts_per_question_are_limited(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        statuses = [self.transcribe(session, turn).status_code for _ in range(7)]
        self.assertEqual(statuses[:6], [200] * 6)
        self.assertEqual(statuses[6], 429)

    def test_a_post_without_a_csrf_token_is_refused(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.profile.user)

        response = strict.post(
            reverse("interviews:transcribe", args=[session.public_id]),
            {"turn": str(turn.pk), "audio": audio()},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(FakeTranscriber.seen_paths, [])


class SubmitTests(VoiceFixture):
    def test_a_spoken_answer_keeps_its_sealed_timings(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        FakeTranscriber.queue(SPOKEN)
        sealed = self.transcribe(session, turn).json()["sealed"]

        self.client.post(
            reverse("interviews:session", args=[session.public_id]),
            {"turn": turn.pk, "answer": SPOKEN.replace("um, ", ""), "delivery": sealed},
        )

        turn.refresh_from_db()
        self.assertEqual(turn.delivery_meta["fillers"], 1)
        self.assertEqual(turn.delivery_meta["words"], len(SPOKEN.split()))
        self.assertNotIn("um,", turn.answer_text)

    def test_an_altered_seal_stores_nothing(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        forged = delivery.sign({"words": 500, "words_per_minute": 150})[:-4] + "AAAA"

        self.client.post(
            reverse("interviews:session", args=[session.public_id]),
            {"turn": turn.pk, "answer": "I did it.", "delivery": forged},
        )

        turn.refresh_from_db()
        self.assertEqual(turn.delivery_meta, {})

    def test_a_typed_interview_ignores_delivery(self):
        session = self.practice_session()
        turn = self.current_turn(session)

        self.client.post(
            reverse("interviews:session", args=[session.public_id]),
            {"turn": turn.pk, "answer": "I did it.", "delivery": delivery.sign({"words": 3})},
        )

        turn.refresh_from_db()
        self.assertEqual(turn.delivery_meta, {})


class EvaluationTests(VoiceFixture):
    def test_delivery_never_reaches_the_evaluator_and_transcription_is_declared(self):
        session = self.voice_session()
        while True:
            with fake_ai():
                FakeAdapter.queue({"question": "What changed after that?"})
                turn = engine.next_question(session=session)
            if turn is None:
                break
            engine.submit_answer(
                session=session,
                turn=turn,
                text=SPOKEN,
                delivery={"words": 25, "words_per_minute": 190, "fillers": 7, "long_pauses": 4},
            )
        session.state = SessionState.COMPLETED
        session.save(update_fields=["state", "updated_at"])
        FakeAdapter.reset()

        with fake_ai():
            FakeAdapter.queue(*([{"score": 3, "rationale": 'Said "rebuilt the nightly".'}] * 10))
            evaluation.evaluate(session=session)

        run = AIRun.objects.filter(task_key="interview.evaluate").first()
        snapshot = str(run.input_snapshot)
        self.assertNotIn("words_per_minute", snapshot)
        self.assertNotIn("190", snapshot)
        self.assertIn("transcribed automatically", run.input_snapshot["answer_mode"])


class DeliverySignalTests(TestCase):
    def transcript(self, spec):
        words = [Word(text=text, start=start, end=end) for text, start, end in spec]
        return Transcript(
            text=" ".join(w.text for w in words), words=words, duration_seconds=60, engine="t"
        )

    def test_pace_pauses_fillers_and_start_delay(self):
        spec = [(f"w{i}", 3.0 + i * 0.5, 3.0 + i * 0.5 + 0.3) for i in range(30)]
        spec[10] = ("um", spec[10][1], spec[10][2])
        # A three-second silence before word 20.
        spec = spec[:20] + [(t, s + 3.0, e + 3.0) for t, s, e in spec[20:]]

        measured = delivery.signals(self.transcript(spec))

        self.assertEqual(measured["words"], 30)
        self.assertEqual(measured["fillers"], 1)
        self.assertEqual(measured["long_pauses"], 1)
        self.assertEqual(measured["start_delay_seconds"], 3.0)
        self.assertEqual(measured["speaking_seconds"], 17.8)
        self.assertEqual(measured["words_per_minute"], round(30 / (17.8 / 60)))

    def test_pace_is_not_reported_for_a_handful_of_words(self):
        spec = [(f"w{i}", i * 0.5, i * 0.5 + 0.3) for i in range(5)]
        self.assertIsNone(delivery.signals(self.transcript(spec))["words_per_minute"])

    def test_feedback_names_the_number_it_comes_from(self):
        notes = delivery.feedback(
            {
                "words": 25,
                "speaking_seconds": 9,
                "words_per_minute": 190,
                "long_pauses": 3,
                "longest_pause_seconds": 4,
                "fillers": 6,
            }
        )

        self.assertTrue(any("25 words" in note for note in notes))
        self.assertTrue(any("190 words a minute" in note for note in notes))
        self.assertTrue(any("3 silences" in note for note in notes))
        self.assertTrue(any("probably higher" in note for note in notes))

    def test_an_ordinary_answer_gets_no_delivery_criticism(self):
        self.assertEqual(
            delivery.feedback(
                {
                    "words": 180,
                    "speaking_seconds": 75,
                    "words_per_minute": 144,
                    "long_pauses": 1,
                    "fillers": 2,
                }
            ),
            [],
        )

    def test_only_known_measurements_are_kept(self):
        cleaned = delivery.clean(
            {"words": "12", "eye_contact": 0.2, "confidence": 9, "fillers": -3}
        )

        self.assertEqual(cleaned["words"], 12)
        self.assertEqual(cleaned["fillers"], 0)
        self.assertNotIn("eye_contact", cleaned)
        self.assertNotIn("confidence", cleaned)


class ResultTests(VoiceFixture):
    def test_a_spoken_result_shows_delivery_as_feedback_not_score(self):
        session = self.voice_session()
        turn = self.current_turn(session)
        engine.submit_answer(
            session=session,
            turn=turn,
            text=SPOKEN,
            delivery={
                "words": 25,
                "speaking_seconds": 9,
                "words_per_minute": 190,
                "long_pauses": 0,
                "fillers": 1,
            },
        )
        engine.abandon(session=session)

        response = self.client.get(reverse("interviews:result", args=[session.public_id]))

        self.assertContains(response, "How you spoke")
        self.assertContains(response, "Not part of your score")
        self.assertContains(response, "190 words a minute")


class SilenceTests(TestCase):
    """A silent recording came back as the word "You"."""

    def segment(self, text, no_speech_prob):
        from types import SimpleNamespace

        words = [
            SimpleNamespace(word=w, start=i * 0.4, end=i * 0.4 + 0.3)
            for i, w in enumerate(text.split())
        ]
        return SimpleNamespace(text=text, words=words, no_speech_prob=no_speech_prob)

    def transcribe(self, *segments):
        from types import SimpleNamespace

        from apps.interviews.speech.local import FasterWhisperTranscriber

        model = mock.Mock()
        model.transcribe.return_value = (iter(segments), SimpleNamespace(duration=6.0))
        with mock.patch.object(FasterWhisperTranscriber, "_model", return_value=model):
            return FasterWhisperTranscriber().transcribe("unused.webm")

    def test_words_whisper_writes_into_silence_are_dropped(self):
        transcript = self.transcribe(self.segment("You", 0.62))
        self.assertEqual(transcript.text, "")
        self.assertEqual(transcript.words, [])

    def test_real_speech_is_kept(self):
        transcript = self.transcribe(self.segment("I rebuilt the reconciliation job.", 0.0))
        self.assertIn("reconciliation", transcript.text)

    def test_only_the_silent_segment_is_dropped(self):
        transcript = self.transcribe(
            self.segment("I rebuilt the job.", 0.01), self.segment("You", 0.7)
        )
        self.assertEqual(transcript.text, "I rebuilt the job.")
