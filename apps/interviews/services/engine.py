"""
The AI text interview (FR-10.1 to FR-10.4, SRS §11.2).

The loop, and what each step is answerable for:

    start          plan the competencies, sample the questions, open the session
    next_question  a planned turn, or a bounded follow-up on what was just said
    submit_answer  persist the turn; evaluation is queued, never awaited
    conclude       aggregate the evaluations into one outcome

**The candidate never waits for scoring.** Evaluation runs per competency after
the session ends, on the `ai` queue. A candidate watching a spinner while a
model reads their answer is a candidate who closes the tab.

**Anti-cheat is framing, not surveillance** (§11.2). A candidate can consult a
model and there is no honest way to stop them, so: questions are time-boxed,
follow-ups reference the candidate's own prior answer — which is hard to fake
cold — and the output is positioned as a screening signal rather than an
assessment. `integrity_meta` records paste and focus events as employer-visible
context and **never reaches the evaluator**. Pretending otherwise produces a
feature whose output is worthless within months of launch.

Every model call goes through `ai.services.runner.run`. Nothing here knows what
a provider is.
"""

from __future__ import annotations

import logging
import random
import re
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from apps.ai.models import RunStatus
from apps.ai.services import runner
from apps.audit import services as audit
from apps.common.errors import InvalidState, NotFound
from apps.notifications import events

from ..models import (
    InterviewKind,
    InterviewOutcome,
    InterviewSession,
    InterviewTemplate,
    InterviewTurn,
    QuestionOrigin,
    Recommendation,
    SessionState,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.applications.models import Application
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

logger = logging.getLogger(__name__)

#: Questions asked per competency from the pool, before follow-ups.
QUESTIONS_PER_COMPETENCY = 2

#: Follow-ups allowed per competency (§11.2). Two, because the third is where an
#: interview stops being a conversation and starts being an interrogation.
MAX_FOLLOWUPS_PER_COMPETENCY = 2

#: Total turns in one session, whatever the plan says. A hard stop so a
#: pathological plan cannot produce a forty-question interview nobody finishes.
MAX_TURNS = 24

#: How long one answer has. Generous — the box exists to shape the interview,
#: not to catch anybody, and a slow connection looks identical to a slow thinker.
ANSWER_SECONDS = 240

#: How much of an answer is kept and sent for scoring. Long enough for anything
#: a person types in four minutes.
MAX_ANSWER_CHARS = 6_000


@transaction.atomic
def start(
    *,
    candidate: CandidateProfile,
    template: InterviewTemplate,
    actor: User,
    application: Application | None = None,
    job: Job | None = None,
    kind: str = InterviewKind.AI_TEXT,
) -> InterviewSession:
    """Open a session and sample its questions (FR-10.1, FR-10.3).

    `application=None` is **practice** (FR-10.2), and that is the whole privacy
    mechanism: a practice session has no application, so no employer queryset
    can reach it. The results are the candidate's by construction rather than by
    a permission check somebody might forget.

    The plan is AI-assisted and the session does not depend on it. With AI off,
    out of budget or the provider down, the template's own pools are sampled and
    the interview runs — degraded to its deterministic parts, which is what
    §9.3.6 asks of every AI feature.
    """
    if application is not None and application.candidate_id != candidate.pk:
        raise InvalidState("That application belongs to somebody else.")
    if kind not in InterviewKind.self_paced():
        raise InvalidState("That is not an interview a candidate takes on their own.")
    if kind == InterviewKind.AI_VOICE and application is not None:
        # Spoken interviews are practice only. An employer screening on voice
        # would be receiving delivery measurements this product has decided
        # are feedback, not evidence.
        raise InvalidState("Spoken interviews are available for practice only.")

    if application is None:
        # A practice session is one unit of the plan's allowance (ADR-A59);
        # an interview an employer invited the candidate to is never metered.
        # Claimed before the session exists and refunded if it cannot start.
        from apps.billing.services import entitlements

        entitlements.claim(candidate, "interview_practice")
    try:
        pools, plan_run, tailored = _plan(template=template, job=job, candidate=candidate)
        if not pools:
            raise InvalidState(
                "This interview template has no questions yet, so there is nothing to ask."
            )
    except Exception:
        if application is None:
            entitlements.release(candidate, "interview_practice")
        raise

    plan = _sample(pools)
    # Whether these questions are this posting's or the general set. A candidate
    # picks a role from a list headed "Which role are you practising for" and
    # gets, on the first run against a new posting, four general competencies —
    # which is the right behaviour and was completely invisible. Recorded on the
    # session so the screen can say which interview they are actually taking.
    # Decided by `_plan`, which already read the kit: asking again queued a
    # second rewrite of a poorly anchored kit for every session started.
    plan["tailored"] = bool(job is not None and tailored)
    if job is not None:
        # The posting this was started against, by name. The session has no
        # foreign key to a job — practice deliberately owns nothing an employer
        # could query — and the screen still has to be able to say which role
        # the candidate chose.
        plan["job_title"] = job.title
        # And its level, so scoring can pitch the rubric at it. A practice
        # session has no application to read the level from later.
        plan["job_level"] = job.get_experience_level_display() or ""

    session = InterviewSession.objects.create(
        application=application,
        candidate=candidate,
        template=template,
        round=None,
        kind=kind,
        state=SessionState.IN_PROGRESS,
        started_at=timezone.now(),
        plan=plan,
        created_by=actor,
        plan_run=plan_run,
    )

    audit.record(
        verb="interview.started",
        actor=actor,
        target=session,
        metadata={"kind": session.kind, "practice": session.is_practice},
    )
    return session


class StaleQuestion(InvalidState):
    """An answer for a question that is no longer the one being asked.

    Raised for the old page a browser's Back button restores, a second tab, a
    form resubmitted after a retry, and the losing request of a double click —
    every case where the question in the form has already been answered or the
    interview has moved on. It is a refusal, never a second answer: nothing is
    written, nothing is scored, no question is created.

    `same_answer` is true when the refused submission carries exactly the answer
    already recorded — a double click or a browser retry of the request that
    succeeded — so the screen can carry on as if it had worked, which it had.
    """

    code = "stale_question"

    def __init__(self, message: str, *, finished: bool = False, same_answer: bool = False):
        super().__init__(message)
        self.finished = finished
        self.same_answer = same_answer


def current_turn(session: InterviewSession) -> InterviewTurn | None:
    """The one question that may be answered now: the earliest unanswered turn.

    Turns are created one at a time, when the previous one is answered or
    skipped, so there is at most one; "earliest" makes that true even for a
    session damaged before this rule existed.
    """
    return session.turns.filter(answer_text="").order_by("order").first()


def _locked(session: InterviewSession) -> InterviewSession:
    """The session row, locked for the rest of the transaction.

    Every change to an interview's progression — answering, skipping, issuing
    the next question, finishing — takes this lock first, so two requests for
    the same interview (a double click, two tabs, a retry racing the original)
    run one after the other and the second sees what the first did.
    """
    list(InterviewSession.objects.select_for_update().filter(pk=session.pk).values_list("pk"))
    # The caller's own object, re-read under the lock, so whatever the caller
    # does with it afterwards sees the committed state rather than a stale copy.
    session.refresh_from_db()
    return session


def _require_current(
    locked: InterviewSession, turn: InterviewTurn, *, answer: str = ""
) -> InterviewTurn:
    """The turn, re-read under the lock, if it is the question being asked now.

    The only submission accepted is one for the current unanswered question of
    an interview that is still running. The check is made on rows read inside
    the session lock, so it cannot be raced.
    """
    if turn.session_id != locked.pk:
        raise NotFound("That question is not part of this interview.")
    list(InterviewTurn.objects.select_for_update().filter(pk=turn.pk).values_list("pk"))
    turn.refresh_from_db()
    fresh = turn
    if locked.is_finished:
        raise StaleQuestion("This interview has already finished.", finished=True)
    if fresh.is_answered:
        raise StaleQuestion(
            "That question has already been answered.",
            same_answer=bool(answer) and fresh.answer_text == answer[:MAX_ANSWER_CHARS],
        )
    current = current_turn(locked)
    if current is None or current.pk != fresh.pk:
        raise StaleQuestion("That is not the question being asked now.")
    return fresh


@transaction.atomic
def next_question(*, session: InterviewSession) -> InterviewTurn | None:
    """The next turn, or `None` when the interview is over.

    Order of preference: an unanswered turn that already exists (a refresh must
    not skip a question), then a follow-up on the last answer if the competency
    has budget for one, then the next planned question.

    Under the session lock: two tabs, or the two pages a double click lands on,
    used to each create "the next question" — the same question twice, as two
    turns — because both read the session before either wrote.
    """
    session = _locked(session)
    if session.is_finished:
        return None

    pending = current_turn(session)
    if pending is not None:
        return pending

    asked = session.turns.count()
    if asked >= MAX_TURNS:
        return None

    followup = _maybe_followup(session)
    if followup is not None:
        return followup

    return _next_planned(session)


@transaction.atomic
def submit_answer(
    *,
    session: InterviewSession,
    turn: InterviewTurn,
    text: str,
    duration_ms: int = 0,
    integrity: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
) -> InterviewTurn:
    """Record one answer (FR-10.4).

    Evaluation is **not** run here. It is queued at `conclude`, per competency,
    so the candidate never waits and so a competency is scored on all of its
    answers rather than one at a time.

    `integrity` is stored and goes no further. The evaluator is not given it.
    """
    answer = (text or "").strip()
    turn = _require_current(_locked(session), turn, answer=answer)

    if not answer:
        raise InvalidState("Write an answer, or skip the question.")

    turn.answer_text = answer[:MAX_ANSWER_CHARS]
    turn.answered_at = timezone.now()
    turn.duration_ms = max(int(duration_ms or 0), 0)
    turn.integrity_meta = _clean_integrity(integrity)
    if session.kind == InterviewKind.AI_VOICE:
        from . import delivery as delivery_service

        turn.delivery_meta = delivery_service.clean(delivery) if delivery else {}
    turn.save(
        update_fields=[
            "answer_text",
            "answered_at",
            "duration_ms",
            "integrity_meta",
            "delivery_meta",
            "updated_at",
        ]
    )
    return turn


@transaction.atomic
def skip(*, session: InterviewSession, turn: InterviewTurn) -> InterviewTurn:
    """Move past a question without answering it.

    A real option, offered deliberately: a candidate stuck on question three of
    twelve either skips it or abandons the interview, and an abandoned interview
    tells the employer nothing about the other nine.
    """
    turn = _require_current(_locked(session), turn, answer="(skipped)")

    turn.answer_text = "(skipped)"
    turn.answered_at = timezone.now()
    turn.save(update_fields=["answer_text", "answered_at", "updated_at"])
    return turn


@transaction.atomic
def conclude(*, session: InterviewSession, actor: User | None = None) -> InterviewSession:
    """End the session and queue its evaluation (FR-10.1).

    Evaluation is queued rather than run inline for the reason at the top of
    this module: the candidate is watching. `conclude` returns as soon as the
    session is closed, and the outcome appears when the scoring finishes.

    Under the session lock and re-checked there, so two requests that both
    reach the end — two tabs, a retry — close it once and queue one evaluation.
    """
    session = _locked(session)
    if session.is_finished:
        return session

    session.state = SessionState.COMPLETED
    session.ended_at = timezone.now()
    session.save(update_fields=["state", "ended_at", "updated_at"])

    events.emit(
        "interview.completed",
        session,
        payload={
            "session_public_id": str(session.public_id),
            "kind": session.kind,
            # Counts only. The outbox has a different retention from the
            # interview and must not accumulate what a candidate said.
            "turns": session.turns.count(),
            "practice": session.is_practice,
        },
    )
    audit.record(
        verb="interview.completed",
        actor=actor,
        target=session,
        metadata={"turns": session.turns.count()},
    )

    transaction.on_commit(lambda: _queue_evaluation(session.pk))
    return session


@transaction.atomic
def abandon(*, session: InterviewSession, reason: str = "") -> InterviewSession:
    """The candidate stopped. Not a failure, and not scored.

    An abandoned session produces no outcome: half an interview is not a weak
    interview, and scoring it would tell an employer something untrue about
    somebody whose connection dropped.
    """
    session = _locked(session)
    if session.is_finished:
        return session

    session.state = SessionState.ABANDONED
    session.ended_at = timezone.now()
    session.cancelled_reason = reason[:200]
    session.save(update_fields=["state", "ended_at", "cancelled_reason", "updated_at"])
    return session


# --- planning -------------------------------------------------------------


def _plan(
    *, template: InterviewTemplate, job: Job | None, candidate: CandidateProfile
) -> tuple[dict[str, Any], Any, bool]:
    """Competencies and their question pools.

    Precedence, and the reasoning behind each step:

      * **A company's own pools win, always.** A recruiter who wrote their
        competency framework should not have a model second-guess it, and the
        scorecard the panel reads afterwards is written against that rubric.
      * **A named job uses that job's interview kit**, when one has been
        written. This is the whole value of practising against a role:
        questions pitched at the level and drawn from what the posting actually
        asks for. The kit is built once per posting by `planning`, off the
        request, and cached — see that module for why it is not built here.
      * **Otherwise the platform pools**, which is general practice: four
        competencies that apply to nearly any role.

    Nothing on this path waits for a model. A candidate pressing Start gets an
    interview immediately, on the best pool that exists at that moment; the
    tailored kit, if it is not written yet, is queued and improves the next one.
    """
    company_authored = template.company_id is not None and template.question_pools
    if company_authored:
        return dict(template.question_pools), None, True

    fallback = dict(template.question_pools or {})
    if job is None:
        return fallback, None, False

    from . import planning

    pools = planning.cached_pools(job)
    if pools:
        return pools, None, True

    _queue_plan(job)
    # No kit yet — and none at all on a deployment with no model. This used to
    # be the four general competencies, identical for every posting, so a
    # Backend Developer and a UI/UX Designer were asked the same questions.
    # The job-aware fallback is built from the posting's own skills,
    # responsibilities and requirements (INT-04). Still "not tailored": the
    # kit, when it lands, is better, and the screen keeps saying so.
    from . import job_context

    return job_context.fallback_pools(job_context.from_job(job), fallback), None, False


def _is_tailored(template: InterviewTemplate, job: Job) -> bool:
    """Whether the questions came from this posting rather than the general set."""
    if template.company_id is not None and template.question_pools:
        return True

    from . import planning

    return bool(planning.cached_pools(job))


def _queue_plan(job: Job) -> None:
    """Ask for this posting's interview kit to be written, off the request.

    Queued rather than awaited, and swallowed rather than raised: a broker that
    is down must not stop somebody practising. The worst case is the interview
    they are about to take uses the general pool, which is what it did anyway.
    """
    from ..tasks import plan_job_interview

    try:
        plan_job_interview.delay(job.pk)
    except Exception:  # noqa: BLE001 - a queue outage is not the candidate's problem
        logger.warning("could not queue an interview kit for job %s", job.pk, exc_info=True)


def _sample(pools: dict[str, Any]) -> dict[str, Any]:
    """Choose this session's questions from the pools (FR-10.3).

    Sampled rather than taken in order, which is the whole of the rotation rule:
    a fixed bank asked in a fixed sequence is on a forum within a month, and
    every candidate after that is answering questions they have already read.

    `random` rather than a seeded shuffle: a seed derived from the session would
    be reproducible, and reproducible sampling is a bank with extra steps.
    """
    plan: dict[str, Any] = {"competencies": []}

    for key, pool in pools.items():
        questions = list(pool.get("questions", []) if isinstance(pool, dict) else pool)
        if not questions:
            continue

        chosen = random.sample(  # noqa: S311 - rotation, not cryptography
            questions, min(QUESTIONS_PER_COMPETENCY, len(questions))
        )
        plan["competencies"].append(
            {
                "key": key,
                "label": (pool.get("label") if isinstance(pool, dict) else "") or key,
                "why": (pool.get("why") if isinstance(pool, dict) else "") or "",
                "questions": chosen,
                "followups": 0,
            }
        )
    return plan


def _next_planned(session: InterviewSession) -> InterviewTurn | None:
    """The next unasked question from the plan."""
    asked = {(turn.competency, turn.question_text) for turn in session.turns.all()}
    order = session.turns.count()

    for competency in session.plan.get("competencies", []):
        for question in competency.get("questions", []):
            if (competency["key"], question) in asked:
                continue
            return InterviewTurn.objects.create(
                session=session,
                order=order,
                competency=competency["key"],
                question_text=question,
                question_origin=QuestionOrigin.POOL,
                started_at=timezone.now(),
                time_limit_seconds=ANSWER_SECONDS,
            )
    return None


def _maybe_followup(session: InterviewSession) -> InterviewTurn | None:
    """A follow-up on the last answer, if this competency has budget.

    Bounded twice: per competency by `MAX_FOLLOWUPS_PER_COMPETENCY`, and by the
    model being unavailable — in which case the interview moves to the next
    planned question rather than stopping. A follow-up is an improvement to the
    interview, not a requirement of it.
    """
    last = session.turns.order_by("-order").first()
    if last is None or not last.is_answered or last.answer_text == "(skipped)":
        return None

    competency = _competency(session, last.competency)
    if competency is None or competency.get("followups", 0) >= MAX_FOLLOWUPS_PER_COMPETENCY:
        return None

    # Only after the planned questions for this competency are done, so a
    # follow-up never displaces a question the employer chose.
    remaining = [
        question
        for question in competency.get("questions", [])
        if not session.turns.filter(competency=last.competency, question_text=question).exists()
    ]
    if remaining:
        return None

    question, run = write_followup(
        competency=competency.get("label") or last.competency,
        question=last.question_text,
        answer=last.answer_text[:MAX_ANSWER_CHARS],
        user=session.candidate.user,
        input_ref=session,
    )
    if not question:
        return None

    if _already_asked(session, question):
        # The model read its own follow-up and asked it again in other words:
        # "What steps did you take after realising you missed the deadline to
        # ensure it wouldn't happen again?" followed by "Can you walk me
        # through the steps you took immediately after missing the deadline to
        # prevent it from happening again?" Being asked the same thing twice is
        # what a candidate remembers about a bad interview, and there is a
        # planned question waiting that is not a repeat.
        logger.info("dropped a follow-up that repeats an earlier question")
        return None

    competency["followups"] = competency.get("followups", 0) + 1
    session.save(update_fields=["plan", "updated_at"])

    return InterviewTurn.objects.create(
        session=session,
        order=session.turns.count(),
        competency=last.competency,
        question_text=question[:400],
        question_origin=QuestionOrigin.AI_FOLLOWUP,
        started_at=timezone.now(),
        time_limit_seconds=ANSWER_SECONDS,
        ai_run=run,
    )


#: How much of a question has to be shared with an earlier one before it is the
#: same question. Two interview questions in the same competency naturally
#: share words — "tell me about a time" — so the bar is high enough that only
#: a restatement trips it.
SAME_QUESTION_SHARE = 0.45


def _already_asked(session: InterviewSession, question: str) -> bool:
    """Whether this question has effectively been asked already."""
    words = _significant(question)
    if not words:
        return True

    for asked in session.turns.values_list("question_text", flat=True):
        earlier = _significant(asked)
        if not earlier:
            continue
        shared = len(words & earlier) / len(words)
        if shared >= SAME_QUESTION_SHARE:
            return True
    return False


#: Words every interview question contains, which carry no signal about what is
#: being asked.
_ASKING_WORDS = frozenset(
    [
        "about",
        "after",
        "again",
        "also",
        "and",
        "any",
        "are",
        "can",
        "could",
        "describe",
        "did",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "into",
        "its",
        "like",
        "make",
        "me",
        "more",
        "most",
        "much",
        "myself",
        "not",
        "now",
        "one",
        "only",
        "our",
        "out",
        "over",
        "said",
        "same",
        "she",
        "some",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "through",
        "time",
        "told",
        "too",
        "walk",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


def _significant(question: str) -> set[str]:
    """The words that say what is being asked, stemmed crudely.

    Four characters, so "missed" and "missing" are the same word and
    "happen" and "happening" are the same word — which is most of what
    separates a restatement from a new question.
    """
    words = re.findall(r"[a-z']+", (question or "").lower())
    return {word[:4] for word in words if len(word) > 2 and word not in _ASKING_WORDS}


def write_followup(
    *, competency: str, question: str, answer: str, user: Any = None, input_ref: Any = None
) -> tuple[str, Any]:
    """One follow-up question for one answer, or `""` when none should be asked.

    Separate from `_maybe_followup` so the benchmark measures exactly what a
    candidate gets — correction detection and the retry included — rather than
    the prompt on its own.
    """
    corrected = corrected_part(answer)
    inputs = {
        "competency": competency,
        "question": question,
        "answer": answer,
        "correction_note": (
            "The candidate corrected themselves partway through. What they say they "
            f'actually did: "{corrected}". Ask about that, not about what they withdrew.'
            if corrected
            else ""
        ),
    }

    run = None
    for attempt in range(2 if corrected else 1):
        run = runner.run(
            task_key="interview.followup", inputs=inputs, user=user, input_ref=input_ref
        )
        if run.status != RunStatus.OK:
            return "", run
        text = one_question(str(run.output.get("question", "")).strip())
        if not corrected or _pulls_on(text, corrected):
            return text, run
        # Measured twice on the benchmark: after "actually, my manager led it —
        # I wrote the rollback scripts" the follow-up asked about the team's
        # migration decisions. One more try, then the planned question.
        logger.info("follow-up ignored a correction (attempt %s)", attempt + 1)
    return "", run


def one_question(text: str) -> str:
    """Keep the first question of a follow-up that asks two.

    The prompt says one question and the local model sent two in 2 of 32
    benchmark follow-ups ("Can you tell me more about the rollback process you
    handled? What challenges did you face?"). Two questions in one turn get one
    answered, and the candidate has to choose which.
    """
    first = text.find("?")
    if first == -1 or first == len(text.rstrip()) - 1:
        return text
    return text[: first + 1].strip()


#: How a candidate takes something back. Matched case-insensitively; the text
#: after the last one is what they now say is true.
_CORRECTIONS = (
    "let me correct that",
    "let me correct myself",
    "to correct myself",
    "correction:",
    "actually, ",
    "actually ",
    "sorry, i mean",
    "i mean, ",
    "no, wait",
    "scratch that",
    "i misspoke",
    "to be honest, ",
)


def corrected_part(answer: str) -> str:
    """What the candidate says after correcting themselves, or "".

    Only when there is enough after the marker to be a statement: "actually
    yes" is not a correction worth steering a follow-up by.
    """
    lowered = (answer or "").lower()
    best = -1
    marker = ""
    for phrase in _CORRECTIONS:
        index = lowered.rfind(phrase)
        if index > best:
            best, marker = index, phrase
    if best < 0:
        return ""
    after = answer[best + len(marker) :].strip(" .,:;-")
    for lead in ("let me correct that", "let me correct myself"):
        if after.lower().startswith(lead):
            after = after[len(lead) :].strip(" .,:;-")
    return after[:400] if len(after.split()) >= 4 else ""


def _pulls_on(question: str, corrected: str) -> bool:
    """Whether a follow-up shares a meaningful word with the corrected statement."""
    return bool(_significant(question) & _significant(corrected))


def _competency(session: InterviewSession, key: str) -> dict[str, Any] | None:
    for competency in session.plan.get("competencies", []):
        if competency.get("key") == key:
            return competency
    return None


def _clean_integrity(integrity: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the counts, drop everything else.

    An allow-list because this is client-supplied and ends up on an employer's
    screen: a pasted payload rendered as "integrity metadata" would be a stored
    XSS vector wearing a security feature's name.
    """
    source = integrity or {}
    return {
        "pastes": max(int(source.get("pastes", 0) or 0), 0),
        "blur_events": max(int(source.get("blur_events", 0) or 0), 0),
        "keystrokes": max(int(source.get("keystrokes", 0) or 0), 0),
    }


def _queue_evaluation(session_id: int) -> None:
    from ..tasks import evaluate_session

    evaluate_session.delay(session_id)


# --- outcome --------------------------------------------------------------


def outcome_for(session: InterviewSession) -> InterviewOutcome | None:
    return getattr(session, "outcome", None)


def recommend(average: float) -> str:
    """Turn an average score into a recommendation.

    Bands rather than a formula, and four rather than a number: a score invites
    averaging across interviewers, which is how a panel's disagreement
    disappears into a mean nobody argued for.
    """
    if average >= 4.3:
        return Recommendation.STRONG_YES
    if average >= 3.4:
        return Recommendation.YES
    if average >= 2.2:
        return Recommendation.NO
    return Recommendation.STRONG_NO
