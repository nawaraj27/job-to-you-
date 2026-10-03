"""
Candidate interview screens (FR-10.1, FR-10.2, FR-10.6, FR-10.11).

Four screens: what is coming up, practice, the interview itself, and the result.

**The interview screen is one question at a time**, deliberately. A page showing
every question at once is a form somebody fills in with the answers open in
another tab, and the time box means nothing. One question, one box, one submit
— which is also what makes a follow-up possible, since it is written after
reading the answer before it.

Everything behind the `interview` flag, which P16 is what finally makes true
(SRS INV-6).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.cache import add_never_cache_headers
from django.utils.decorators import method_decorator
from django.views import View

from apps.billing.services.entitlements import PlanLimitExceeded
from apps.candidates.services import profile as profile_service
from apps.common import restrictions
from apps.common.errors import DomainError
from apps.common.ids import as_pk
from apps.core.features import is_enabled
from apps.interviews import selectors, speech
from apps.interviews.models import InterviewKind, InterviewSession, SessionState
from apps.interviews.services import delivery, engine, evaluation, rooms, scheduling


class CandidateInterviewView(View):
    """Base: signed in, the flag on, and a profile."""

    @method_decorator(login_required)
    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if not is_enabled("interview"):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def profile(self, request: HttpRequest):
        return profile_service.get_or_create_profile(user=request.user)

    def session(self, request: HttpRequest, public_id: str):
        found = selectors.session_for_candidate(self.profile(request), public_id)
        if found is None:
            # `NotFound` rather than a 403: a 403 on somebody else's interview
            # confirms that the interview exists.
            raise Http404("That interview is not yours.")
        return found


class InterviewHubView(CandidateInterviewView):
    """`/interviews/` — what is scheduled, and what has been practised."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        return render(
            request,
            "interviews/hub.html",
            {
                "page_title": "Interviews",
                "upcoming": selectors.upcoming_for_candidate(profile),
                "practice": selectors.practice_for(profile)[:10],
                "history": selectors.sessions_for_candidate(profile).filter(
                    application__isnull=False
                )[:10],
                "templates": selectors.practice_templates(),
                "practice_jobs": _practice_jobs(profile),
                "speech_available": speech.is_available(),
                # The plan's practice allowance, shown before anybody presses
                # Start (ADR-A59).
                "allowance": _practice_allowance(profile),
            },
        )


def _practice_allowance(profile) -> dict:
    from apps.billing.services import entitlements

    allowed = entitlements.value_of(profile, "interview_practice")
    return {
        "allowed": allowed,
        "remaining": entitlements.remaining(profile, "interview_practice"),
        "resets_on": entitlements.resets_on("interview_practice"),
    }


#: How many roles the practice picker offers. Long enough to include the ones
#: they are actually chasing, short enough to stay a choice rather than a
#: search.
PRACTICE_JOB_LIMIT = 25


def _practice_jobs(profile) -> list:
    """Roles worth practising for, most relevant first.

    A candidate practising has one of three things in mind, in this order: an
    interview they already have, a job they saved, or a job they might apply
    to. Offering the whole board would be a search box; offering nothing — the
    previous behaviour — meant every practice interview asked the same four
    generic questions no matter which role the candidate was chasing.
    """
    from apps.jobs.models import Job, JobStatus

    chosen: list = []
    seen: set[int] = set()

    def take(queryset) -> None:
        for job in queryset.select_related("company"):
            if job.pk not in seen and len(chosen) < PRACTICE_JOB_LIMIT:
                seen.add(job.pk)
                chosen.append(job)

    live = Job.objects.filter(status=JobStatus.PUBLISHED, deleted_at__isnull=True)
    take(live.filter(applications__candidate=profile).distinct())
    take(live.filter(saved_by__profile=profile).distinct())
    take(live.order_by("-published_at"))
    return chosen


class PracticeStartView(CandidateInterviewView):
    """`/interviews/practice/` — start a practice run (FR-10.2).

    A POST, because it creates a session and samples its questions. A GET that a
    refresh repeated would leave a trail of abandoned interviews behind it.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        template = selectors.practice_template(request.POST.get("template", "general-practice"))
        if template is None:
            raise Http404("No such practice interview.")

        job = _practice_job(request.POST.get("job") or "")

        kind = (
            InterviewKind.AI_VOICE if request.POST.get("mode") == "voice" else InterviewKind.AI_TEXT
        )
        unfinished = _unfinished_practice(profile, template=template, job=job, kind=kind)
        if unfinished is not None:
            # Pressing Start again — a second tab, the back button, a double
            # click — opened another interview every time. The hub had five
            # "in progress" rehearsals for one candidate, each one question in.
            return redirect("interviews:session", public_id=unfinished.public_id)

        try:
            session = engine.start(
                candidate=profile, template=template, actor=request.user, job=job, kind=kind
            )
        except PlanLimitExceeded as limit:
            # The plan's practice allowance: a normal state, said as a
            # restriction with its reset date and a way on (ADR-A59).
            restrictions.flash(request, limit)
            return redirect("interviews:hub")
        except DomainError as exc:
            return render(
                request,
                "interviews/hub.html",
                {"page_title": "Interviews", "error": exc.message},
                status=400,
            )

        return redirect("interviews:session", public_id=session.public_id)


def _practice_job(public_id: str):
    """The job a practice session is aimed at, if the candidate named one.

    Only a published job, and looked up without reference to the candidate:
    practising for a role is not an application and needs no relationship to
    it. An id that does not resolve is treated as "no job" rather than as a
    404 — a stale picker should start a general practice, not refuse.
    """
    from apps.common.ids import as_uuid
    from apps.jobs.models import Job, JobStatus

    public_id = as_uuid(public_id)
    if public_id is None:
        return None

    return Job.objects.filter(
        public_id=public_id, status=JobStatus.PUBLISHED, deleted_at__isnull=True
    ).first()


class InterviewSessionView(CandidateInterviewView):
    """`/interviews/<id>/` — the interview itself.

    One question at a time. The GET asks for the next one, which is also what
    creates it: a question that existed before the candidate arrived at it would
    have to be timed from somewhere, and there is nowhere honest to time it from.
    """

    def get(self, request: HttpRequest, public_id: str) -> HttpResponse:
        session = self.session(request, public_id)

        if session.is_finished:
            return redirect("interviews:result", public_id=session.public_id)
        if session.kind not in InterviewKind.self_paced():
            return redirect("interviews:result", public_id=session.public_id)

        turn = engine.next_question(session=session)
        if turn is None:
            engine.conclude(session=session, actor=request.user)
            return redirect("interviews:result", public_id=session.public_id)

        response = render(request, "interviews/session.html", _turn_context(session, turn))
        # Every question is served from the one URL, so a cached copy is a copy
        # of a question that may already be answered. Not stored means Back and
        # refresh ask the server, which shows the question being asked now.
        add_never_cache_headers(response)
        return response

    def post(self, request: HttpRequest, public_id: str) -> HttpResponse:
        session = self.session(request, public_id)
        turn = session.turns.filter(pk=as_pk(request.POST.get("turn"))).first()
        if turn is None:
            raise Http404("That question is not part of this interview.")

        try:
            if request.POST.get("action") == "skip":
                engine.skip(session=session, turn=turn)
            else:
                engine.submit_answer(
                    session=session,
                    turn=turn,
                    text=request.POST.get("answer", ""),
                    # Measured here from when the question was first shown.
                    # The browser's figure restarted on every reload, so an
                    # answer written across a refresh reported only the part
                    # after it.
                    duration_ms=_elapsed_ms(turn),
                    integrity=_integrity(request.POST.get("integrity")),
                    # The timings were measured and sealed when the recording
                    # was transcribed; an altered or stale seal carries nothing.
                    delivery=delivery.unsign(request.POST.get("delivery", "")),
                )
        except engine.StaleQuestion as exc:
            return _stale_answer(request, session, turn, exc)
        except DomainError as exc:
            return render(
                request,
                "interviews/session.html",
                {
                    **_turn_context(session, turn),
                    "error": exc.message,
                    # What they typed comes back with the error rather than
                    # vanishing with it.
                    "draft": request.POST.get("answer", ""),
                },
                status=400,
            )

        return redirect("interviews:session", public_id=session.public_id)


def _stale_answer(request, session, turn, exc) -> HttpResponse:
    """The answer to an answer for a question that is no longer being asked.

    Nothing was written. A double click or a browser retry of the submission
    that already succeeded carries the recorded answer itself, and simply goes
    on to the current question — from the candidate's side it worked, once.
    Anything else — the page Back restored, a second tab, a form resubmitted
    after the interview moved on — gets a page that says so and a way on, never
    the old question dressed up as live.
    """
    if exc.same_answer and not exc.finished:
        return redirect("interviews:session", public_id=session.public_id)

    session.refresh_from_db()
    response = render(
        request,
        "interviews/stale.html",
        {
            "page_title": "Interview",
            "session": session,
            "turn": turn,
            "finished": session.is_finished,
            "unsaved": (request.POST.get("answer") or "").strip(),
            "message": exc.message,
        },
        status=409,
    )
    add_never_cache_headers(response)
    return response


class InterviewFinishView(CandidateInterviewView):
    """`/interviews/<id>/finish/` — stop early, on purpose."""

    def post(self, request: HttpRequest, public_id: str) -> HttpResponse:
        session = self.session(request, public_id)

        if request.POST.get("action") == "abandon":
            engine.abandon(session=session, reason="Ended by the candidate")
        elif session.turns.exclude(answer_text="").exclude(answer_text="(skipped)").exists():
            engine.conclude(session=session, actor=request.user)
        else:
            # Finishing with nothing answered would queue an evaluation of
            # nothing and leave the result page waiting for it forever.
            engine.abandon(session=session, reason="Ended by the candidate before answering")

        return redirect("interviews:result", public_id=session.public_id)


def _practice_verdict(scores: list) -> dict:
    """What a rehearsal should say instead of a hiring decision.

    A practice interview was headlined "No — 2.50 / 5". Nobody is hiring: it is
    a private rehearsal nobody else can see, and the one thing a candidate can
    act on is which answer let them down and which one worked. A hire/no-hire
    verdict on your own practice is discouraging and, worse, is not a thing to
    do anything about.

    The wording stays honest about the number — it is the same scoring an
    employer would see, which is the point of practising — but leads with the
    part that tells you what to go and fix.
    """
    graded = [score for score in scores if score.score]
    if not graded:
        return {}

    ranked = sorted(graded, key=lambda score: score.score)
    return {
        "practice_weakest": ranked[0],
        "practice_strongest": ranked[-1] if len(ranked) > 1 else None,
    }


class InterviewResultView(CandidateInterviewView):
    """`/interviews/<id>/result/` — what the candidate is allowed to see.

    Practice is always theirs. An employer interview shows the result only once
    the employer shared it (FR-10.11) — and says so plainly when they have not,
    because "we'll be in touch" with no explanation is the thing this feature
    exists to improve on.
    """

    def get(self, request: HttpRequest, public_id: str) -> HttpResponse:
        session = self.session(request, public_id)
        outcome = evaluation.readable_by_candidate(session)
        unscored = (
            outcome is None
            and session.state == SessionState.COMPLETED
            and not session.evaluations.exists()
        )
        scores = (
            list(session.evaluations.filter(submitted_at__isnull=False).order_by("competency"))
            if outcome is not None
            else []
        )

        return render(
            request,
            "interviews/result.html",
            {
                "page_title": "Interview result",
                "session": session,
                "outcome": outcome,
                "scores": scores,
                # What to do next, for a rehearsal. See `_practice_verdict`.
                **(_practice_verdict(scores) if session.is_practice else {}),
                "turns": _with_delivery_notes(session),
                "delivery_summary": (
                    delivery.summarise(list(session.turns.all()))
                    if session.kind == InterviewKind.AI_VOICE
                    else {}
                ),
                # "Being read" and "they chose not to share" are different
                # states and a candidate must not see the first forever because
                # the second happened. `pending` means scoring has not finished;
                # an existing-but-unshared outcome is not pending.
                "pending": unscored and not _scoring_given_up(session),
                # Retried for a day by `recover_unscored_sessions`; after that
                # "being read" would be untrue, so the page says what happened.
                "unread": unscored and _scoring_given_up(session),
            },
        )


def _scoring_given_up(session: InterviewSession) -> bool:
    """Whether the recovery sweep has stopped asking for this interview's score."""
    from django.utils import timezone

    from apps.interviews.tasks import SCORING_RETRY_HOURS

    ended = session.ended_at or session.updated_at
    return ended < timezone.now() - dt.timedelta(hours=SCORING_RETRY_HOURS)


class InterviewJoinView(CandidateInterviewView):
    """`/interviews/<id>/join/` — the room for a live interview (FR-10.5).

    The room opens fifteen minutes before and closes fifteen after: earlier and
    it is a place to loiter, later and somebody who mistook the hour joins an
    interview that already ended.
    """

    def get(self, request: HttpRequest, public_id: str) -> HttpResponse:
        session = self.session(request, public_id)

        try:
            session = scheduling.record_join(session=session, user=request.user)
            join_url = rooms.join_url(session=session, user=request.user, is_host=False)
        except DomainError as exc:
            return render(
                request,
                "interviews/join.html",
                {"page_title": "Join", "session": session, "error": exc.message},
                status=409,
            )

        return render(
            request,
            "interviews/join.html",
            {"page_title": "Join", "session": session, "join_url": join_url},
        )


class InterviewSlotsView(CandidateInterviewView):
    """`/interviews/book/<application>/` — pick a time (FR-10.6, FR-10.7).

    The same screen books a first interview and moves an existing one, because
    to the candidate they are one action: choose when. Which of the two it is
    depends on whether they already have a live session, not on which link they
    followed.
    """

    def _booked(self, application):
        return (
            application.interview_sessions.filter(
                state__in=(SessionState.SCHEDULED, SessionState.IN_PROGRESS)
            )
            .order_by("scheduled_at")
            .first()
        )

    def _context(self, application) -> dict:
        booked = self._booked(application)
        return {
            "page_title": "Move your interview" if booked else "Book your interview",
            "application": application,
            "booked": booked,
            "slots": scheduling.open_slots(company=application.job.company)[:40],
        }

    def get(self, request: HttpRequest, application_id: str) -> HttpResponse:
        application = self._application(request, application_id)
        return render(request, "interviews/book.html", self._context(application))

    def post(self, request: HttpRequest, application_id: str) -> HttpResponse:
        application = self._application(request, application_id)
        slot = (
            scheduling.open_slots(company=application.job.company)
            .filter(pk=as_pk(request.POST.get("slot")))
            .first()
        )
        if slot is None:
            raise Http404("That time is no longer available.")

        template = selectors.live_template()
        if template is None:
            raise Http404("No interview template is configured.")

        booked = self._booked(application)

        try:
            if booked is not None:
                # Moving an interview is a reschedule, not a second booking.
                # Without this branch a candidate who came back to change their
                # time ended up with two live sessions, two sets of reminders,
                # and an interviewer double-booked against themselves.
                session = scheduling.reschedule(
                    session=booked,
                    slot=slot,
                    actor=request.user,
                    reason=request.POST.get("reason", "Moved by the candidate"),
                )
            else:
                session = scheduling.book(
                    slot=slot, application=application, actor=request.user, template=template
                )
        except DomainError as exc:
            return render(
                request,
                "interviews/book.html",
                {**self._context(application), "error": exc.message},
                status=409,
            )

        return redirect("interviews:result", public_id=session.public_id)

    def _application(self, request: HttpRequest, application_id: str):
        from apps.applications import selectors as application_selectors

        application = application_selectors.application_of(self.profile(request), application_id)
        if application is None:
            raise Http404("That application is not yours.")
        return application


#: A four-minute answer in Opus is well under 2 MB; this allows lossless
#: recordings from browsers that will not compress, and refuses anything else.
MAX_AUDIO_BYTES = 8 * 1024 * 1024

#: Transcription attempts per question, so one tab cannot occupy the speech
#: engine indefinitely.
MAX_TRANSCRIBE_ATTEMPTS = 6

_AUDIO_SUFFIX = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
}


class TranscribeAnswerView(CandidateInterviewView):
    """`/interviews/<id>/transcribe/` — one spoken answer in, its text out.

    **The audio is deleted before this returns.** It is written to a temporary
    file because the decoder reads from a path, and removed in `finally`
    whatever happened. Nothing is written to media storage, nothing is logged
    about its content, and the camera is never involved: the browser records
    the microphone track only.

    Returns JSON, because the page calling it is mid-interview and a redirect or
    an error page would lose the candidate's place.
    """

    def post(self, request: HttpRequest, public_id: str) -> HttpResponse:
        import contextlib
        import os
        import tempfile

        from django.core.cache import cache
        from django.http import JsonResponse

        session = self.session(request, public_id)
        if session.kind != InterviewKind.AI_VOICE or session.is_finished:
            return JsonResponse(
                {"error": "This interview is not taking spoken answers."}, status=409
            )

        turn = session.turns.filter(pk=_as_int(request.POST.get("turn"))).first()
        if turn is None:
            return JsonResponse(
                {"error": "That question is not part of this interview."}, status=404
            )
        current = engine.current_turn(session)
        if current is None or current.pk != turn.pk:
            # A recording made on a page Back restored, or in a second tab,
            # for a question already answered. Not transcribed — and even if
            # it were, the answer would be refused (`engine.StaleQuestion`).
            return JsonResponse(
                {
                    "error": "That question has already been answered. Reload to continue.",
                    "stale": True,
                },
                status=409,
            )

        upload = request.FILES.get("audio")
        if upload is None or not upload.size:
            return JsonResponse({"error": "No recording arrived. Record again."}, status=400)
        if upload.size > MAX_AUDIO_BYTES:
            return JsonResponse(
                {
                    "error": "That recording is too long to transcribe. Keep answers under four minutes."
                },
                status=413,
            )
        content_type = (upload.content_type or "").split(";")[0].strip().lower()
        if not content_type.startswith("audio/"):
            return JsonResponse({"error": "That was not an audio recording."}, status=415)

        attempts_key = f"interviews:transcribe:{turn.pk}"
        cache.add(attempts_key, 0, timeout=60 * 60)
        if cache.incr(attempts_key) > MAX_TRANSCRIBE_ATTEMPTS:
            return JsonResponse(
                {"error": "That is a lot of attempts on one question. Type your answer instead."},
                status=429,
            )

        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed before reading, removed in finally
            suffix=_AUDIO_SUFFIX.get(content_type, ".audio"), delete=False
        )
        try:
            for chunk in upload.chunks():
                handle.write(chunk)
            handle.close()
            transcript = speech.transcriber().transcribe(handle.name)
        except speech.SpeechUnavailable:
            return JsonResponse(
                {"error": "Speech recognition is not available right now. Type your answer below."},
                status=503,
            )
        except speech.SpeechFailed:
            return JsonResponse(
                {
                    "error": "The recording could not be understood. Record again, or type your answer."
                },
                status=422,
            )
        finally:
            handle.close()
            with contextlib.suppress(OSError):
                os.remove(handle.name)

        if not transcript.text.strip():
            return JsonResponse(
                {"error": "No speech was heard. Check your microphone and record again."},
                status=422,
            )

        measured = delivery.signals(transcript)
        return JsonResponse(
            {
                "text": transcript.text,
                "measured": measured,
                "sealed": delivery.sign(measured),
                "feedback": delivery.feedback(measured),
            }
        )


# --- helpers --------------------------------------------------------------


#: How recently an unfinished practice has to have started to be resumed rather
#: than replaced. Stale sessions are closed by `close_stale_sessions` at six.
RESUME_HOURS = 6


def _unfinished_practice(profile, *, template, job, kind=InterviewKind.AI_TEXT):
    import datetime as dt

    from django.utils import timezone

    candidates = selectors.practice_for(profile).filter(
        state=SessionState.IN_PROGRESS,
        template=template,
        kind=kind,
        started_at__gte=timezone.now() - dt.timedelta(hours=RESUME_HOURS),
    )
    title = job.title if job is not None else None
    for session in candidates[:10]:
        if (session.plan or {}).get("job_title") == title:
            return session
    return None


def _elapsed_ms(turn) -> int:
    from django.utils import timezone

    if turn.started_at is None:
        return 0
    return max(int((timezone.now() - turn.started_at).total_seconds() * 1000), 0)


def _turn_context(session, turn) -> dict:
    limit = turn.time_limit_seconds or engine.ANSWER_SECONDS
    remaining = max(limit - _elapsed_ms(turn) // 1000, 0)
    return {
        "page_title": "Interview",
        "session": session,
        "turn": turn,
        "answered": session.turns.exclude(answer_text="").count(),
        "planned": _planned_total(session),
        "seconds": remaining,
        "clock": f"{remaining // 60}:{remaining % 60:02d}",
        "voice": session.kind == InterviewKind.AI_VOICE,
        "speech_available": speech.is_available(),
    }


def _with_delivery_notes(session) -> list:
    turns = list(selectors.turns_for(session))
    for turn in turns:
        turn.delivery_notes = delivery.feedback(turn.delivery_meta or {})
    return turns


def _planned_total(session) -> int:
    """How many questions this interview can ask, follow-ups included.

    The follow-up allowance is part of the plan and was not counted, so a
    session with eight planned questions and five allowed follow-ups told the
    candidate "Question 9 of about 8" — and then 10, and 11. "About" is doing
    honest work here: not every allowance is spent, so the real number lands at
    or under this one.
    """
    competencies = session.plan.get("competencies", [])
    return sum(
        len(row.get("questions", [])) + int(row.get("followups", 0) or 0) for row in competencies
    )


def _as_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _integrity(raw: Any) -> dict[str, Any]:
    """Parse the client's paste and focus counts.

    Anything unparseable is an empty dict rather than an error: this is
    telemetry, and a browser that sent nonsense should not fail a candidate's
    interview. The service allow-lists the keys regardless.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
