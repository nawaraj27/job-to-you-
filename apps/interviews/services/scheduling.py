"""
Scheduling a live interview (FR-10.6, FR-10.7, SRS §11.3).

The shape, and where the guarantee lives:

    publish   an interviewer opens windows on their calendar
    hold      a candidate picks one; a short hold covers the confirmation
    book      the hold becomes a session; **Postgres rejects an overlap**
    move      reschedule, cancel, no-show — the majority of real traffic

**Double-booking is prevented by the database.** `InterviewSlot` carries a GiST
exclusion constraint over `(interviewer, tstzrange(starts_at, ends_at))` where
the slot is not cancelled. Two candidates confirming the same window in the same
millisecond is not a rare case — it is what happens when a reminder goes out and
everybody opens the link at once — and a check-then-insert would let both
through. This module turns the `IntegrityError` into a sentence somebody can
read; it does not attempt to prevent the race itself.

**Reschedule is one act, not a cancel and a book.** `rescheduled_from` keeps the
thread, because both sides need to see continuity rather than a cancellation
followed by a surprise.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.notifications import events

from ..models import (
    InterviewerAvailability,
    InterviewKind,
    InterviewSession,
    InterviewSlot,
    InterviewTemplate,
    ParticipantRole,
    SessionState,
    SlotState,
)
from ..providers import room_provider

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.applications.models import Application
    from apps.companies.models import CompanyMembership

logger = logging.getLogger(__name__)

#: How long a hold lasts. Long enough to confirm, short enough that a closed tab
#: does not cost somebody else the slot.
HOLD_MINUTES = 10

#: The default length of a published window.
DEFAULT_SLOT_MINUTES = 45

#: How far ahead slots may be published. A year of open slots is a calendar
#: nobody maintains and a candidate booking into a job that closed in March.
MAX_HORIZON_DAYS = 90


@transaction.atomic
def publish_slots(
    *,
    membership: CompanyMembership,
    actor: User,
    windows: list[tuple[dt.datetime, dt.datetime]],
) -> list[InterviewSlot]:
    """Open bookable windows on one interviewer's calendar.

    An overlap with their own existing slot is refused by the same constraint
    that refuses a double booking — the interviewer publishing two overlapping
    windows is the same mistake as two candidates taking one, and it is caught
    in the same place.
    """
    horizon = timezone.now() + dt.timedelta(days=MAX_HORIZON_DAYS)
    created: list[InterviewSlot] = []

    for starts_at, ends_at in windows:
        if ends_at <= starts_at:
            raise InvalidState("A slot has to end after it starts.")
        if starts_at < timezone.now():
            raise InvalidState("A slot cannot be in the past.")
        if starts_at > horizon:
            raise InvalidState(f"Slots can be published up to {MAX_HORIZON_DAYS} days ahead.")

        try:
            with transaction.atomic():
                created.append(
                    InterviewSlot.objects.create(
                        interviewer=membership, starts_at=starts_at, ends_at=ends_at
                    )
                )
        except IntegrityError as exc:
            raise Conflict(
                f"You already have something between {starts_at:%d %b %H:%M} and {ends_at:%H:%M}."
            ) from exc

    audit.record(
        verb="interview.slots_published",
        actor=actor,
        target=membership.company,
        company=membership.company,
        metadata={"count": len(created)},
    )
    return created


def open_slots(*, company: Any, since: dt.datetime | None = None) -> QuerySet[InterviewSlot]:
    """Slots a candidate could take, for one company.

    Includes slots whose hold has expired: a hold that nobody confirmed is a
    slot that is free again, and sweeping it lazily on read means no scheduled
    job has to be running for booking to work.
    """
    now = timezone.now()
    return (
        InterviewSlot.objects.filter(
            interviewer__company=company,
            starts_at__gte=since or now,
        )
        .filter(Q(state=SlotState.OPEN) | Q(state=SlotState.HELD, held_until__lt=now))
        .select_related("interviewer", "interviewer__user")
        .order_by("starts_at", "pk")
    )


@transaction.atomic
def hold(*, slot: InterviewSlot, actor: User) -> InterviewSlot:
    """Take a slot off the market while somebody confirms (FR-10.6).

    `select_for_update` because two candidates clicking the same slot a
    millisecond apart must not both get a hold. The exclusion constraint would
    catch the double *booking* later, but by then one of them has filled in a
    form for a slot they were never going to get.
    """
    locked = InterviewSlot.objects.select_for_update().get(pk=slot.pk)
    if not locked.is_bookable:
        raise Conflict("Somebody just took that time. Pick another.")

    locked.state = SlotState.HELD
    locked.held_until = timezone.now() + dt.timedelta(minutes=HOLD_MINUTES)
    locked.save(update_fields=["state", "held_until", "updated_at"])
    return locked


@transaction.atomic
def book(
    *,
    slot: InterviewSlot,
    application: Application,
    actor: User,
    template: InterviewTemplate,
    interviewers: list[User] | None = None,
    announce: bool = True,
) -> InterviewSession:
    """Turn a held slot into a scheduled interview (FR-10.6).

    `announce=False` is for `reschedule`, which says "Interview moved" itself:
    a move used to reach both sides twice, "Interview booked" and then
    "Interview moved", for the one change.

    The `IntegrityError` branch is the load-bearing one: it is the exclusion
    constraint refusing a second booking for a window somebody else already
    holds. Catching it here turns a database error into "somebody just took that
    time", which is what actually happened.
    """
    locked = InterviewSlot.objects.select_for_update().get(pk=slot.pk)
    if locked.state == SlotState.BOOKED:
        raise Conflict("Somebody just took that time. Pick another.")
    if locked.state == SlotState.CANCELLED:
        raise NotFound("That time is no longer available.")

    room = room_provider().create_room(
        subject=f"Interview: {application.job.title}",
        starts_at=locked.starts_at,
        ends_at=locked.ends_at,
    )

    session = InterviewSession.objects.create(
        application=application,
        candidate=application.candidate,
        template=template,
        kind=InterviewKind.LIVE,
        state=SessionState.SCHEDULED,
        scheduled_at=locked.starts_at,
        provider=room.provider,
        room_ref=room.room_ref,
        join_token_hash=room.token_hash,
        created_by=actor,
    )

    try:
        with transaction.atomic():
            locked.state = SlotState.BOOKED
            locked.held_until = None
            locked.session = session
            locked.save(update_fields=["state", "held_until", "session", "updated_at"])
    except IntegrityError as exc:
        raise Conflict("Somebody just took that time. Pick another.") from exc

    _invite(session, interviewers or [locked.interviewer.user])

    if announce:
        _announce_booking(session, application, locked)
    audit.record(
        verb="interview.scheduled",
        actor=actor,
        target=session,
        company=application.job.company,
        metadata={"starts_at": locked.starts_at.isoformat()},
    )
    return session


def _announce_booking(
    session: InterviewSession, application: Application, locked: InterviewSlot
) -> None:
    events.emit(
        "interview.scheduled",
        session,
        payload={
            "session_public_id": str(session.public_id),
            "candidate_public_id": str(application.candidate.public_id),
            "kind": session.kind,
            "when": when_text(locked.starts_at),
            "starts_at": locked.starts_at.isoformat(),
            "company": application.job.company.name,
            "job": application.job.title,
        },
    )


@transaction.atomic
def reschedule(
    *, session: InterviewSession, slot: InterviewSlot, actor: User, reason: str = ""
) -> InterviewSession:
    """Move an interview to another slot (FR-10.7).

    A new session linked by `rescheduled_from`, not an edit: the old session is
    what the reminders, the ICS invite and the timeline already refer to, and
    rewriting its time would leave every one of them describing something that
    is not happening. The thread is what both sides need to see.
    """
    if session.is_finished:
        raise InvalidState("That interview has already happened.")
    if session.state == SessionState.IN_PROGRESS:
        raise InvalidState("That interview is under way. It can be moved once it has ended.")
    application = session.application
    if application is None:
        raise InvalidState("A practice session is not scheduled.")

    moved = book(
        slot=slot,
        application=application,
        actor=actor,
        template=session.template,
        interviewers=[row.user for row in session.participants.all()],
        announce=False,
    )
    moved.rescheduled_from = session
    moved.save(update_fields=["rescheduled_from", "updated_at"])

    _release(session)
    session.state = SessionState.CANCELLED
    session.cancelled_reason = (reason or "Rescheduled")[:200]
    session.ended_at = timezone.now()
    session.save(update_fields=["state", "cancelled_reason", "ended_at", "updated_at"])

    events.emit(
        "interview.rescheduled",
        moved,
        payload={
            "session_public_id": str(moved.public_id),
            "candidate_public_id": str(moved.candidate.public_id),
            "from_public_id": str(session.public_id),
            "when": when_text(slot.starts_at),
            "starts_at": slot.starts_at.isoformat(),
            "company": application.job.company.name,
            "job": application.job.title,
        },
    )
    return moved


@transaction.atomic
def cancel(*, session: InterviewSession, actor: User, reason: str = "") -> InterviewSession:
    """Call an interview off (FR-10.7).

    The slot goes back on the market rather than staying blocked: an interviewer
    whose cancelled interviews silently consumed their calendar would stop
    publishing availability within a week.
    """
    if session.is_finished:
        raise InvalidState("That interview has already finished.")

    session.state = SessionState.CANCELLED
    session.cancelled_reason = reason.strip()[:200]
    session.ended_at = timezone.now()
    session.save(update_fields=["state", "cancelled_reason", "ended_at", "updated_at"])
    _release(session)

    events.emit(
        "interview.cancelled",
        session,
        payload={
            "session_public_id": str(session.public_id),
            "candidate_public_id": str(session.candidate.public_id),
            "reason": session.cancelled_reason or "No reason given",
            "company": (
                session.application.job.company.name if session.application is not None else ""
            ),
            "job": session.application.job.title if session.application is not None else "",
        },
    )
    audit.record(
        verb="interview.cancelled", actor=actor, target=session, metadata={"reason": reason[:200]}
    )
    return session


@transaction.atomic
def mark_no_show(
    *, session: InterviewSession, actor: User, missing_side: str = "candidate"
) -> InterviewSession:
    """Nobody came (FR-10.7).

    `missing_side` is recorded because a candidate no-show and an interviewer
    no-show are different failures with different apologies owed, and a single
    "no_show" state that did not say which would make the difference invisible
    to everybody afterwards.
    """
    if session.is_finished:
        raise InvalidState("That interview has already finished.")
    if missing_side not in {"candidate", "interviewer", "both"}:
        raise InvalidState("Say who did not come.")
    if session.scheduled_at is not None and session.scheduled_at > timezone.now():
        # Recording a no-show for Friday's interview on Tuesday closes the
        # interview, frees the slot and tells the candidate nothing.
        raise InvalidState("A no-show can be recorded once the interview time has arrived.")

    session.state = SessionState.NO_SHOW
    session.ended_at = timezone.now()
    session.cancelled_reason = f"No show: {missing_side}"
    session.save(update_fields=["state", "ended_at", "cancelled_reason", "updated_at"])
    _release(session)

    events.emit(
        "interview.no_show",
        session,
        payload={
            "session_public_id": str(session.public_id),
            "missing_side": missing_side,
        },
    )
    audit.record(
        verb="interview.no_show",
        actor=actor,
        target=session,
        metadata={"missing_side": missing_side},
    )
    return session


@transaction.atomic
def record_join(*, session: InterviewSession, user: User) -> InterviewSession:
    """Somebody entered the room (FR-10.5).

    The first entry moves the interview to `in_progress`; an interviewer entry
    marks them attended. Nothing moved a live interview out of `scheduled`
    before this, so a live interview that happened still read "Scheduled" to
    both sides forever, never appeared under "Done", and could not be told apart
    from one nobody came to.
    """
    from .. import policies
    from ..models import InterviewParticipant

    locked = InterviewSession.objects.select_for_update().get(pk=session.pk)
    policies.require_joinable(locked)

    if locked.state == SessionState.SCHEDULED:
        locked.state = SessionState.IN_PROGRESS
        locked.started_at = timezone.now()
        locked.save(update_fields=["state", "started_at", "updated_at"])

    InterviewParticipant.objects.filter(session=locked, user=user).update(
        attended=True, updated_at=timezone.now()
    )
    return locked


@transaction.atomic
def complete_live(*, session: InterviewSession, actor: User | None) -> InterviewSession:
    """A live interview has ended (FR-10.10).

    Called when the panel records the outcome. Refused before the interview
    time, because an outcome for a conversation that has not happened is a
    decision made without one.
    """
    locked = InterviewSession.objects.select_for_update().get(pk=session.pk)
    if locked.kind != InterviewKind.LIVE:
        raise InvalidState("Only a live interview is completed this way.")
    if locked.state == SessionState.COMPLETED:
        return locked
    if locked.state not in {SessionState.SCHEDULED, SessionState.IN_PROGRESS}:
        raise InvalidState("That interview did not take place, so it cannot be completed.")
    if locked.scheduled_at is not None and locked.scheduled_at > timezone.now():
        raise InvalidState("That interview has not happened yet.")

    now = timezone.now()
    locked.state = SessionState.COMPLETED
    locked.started_at = locked.started_at or locked.scheduled_at or now
    locked.ended_at = now
    locked.save(update_fields=["state", "started_at", "ended_at", "updated_at"])

    events.emit(
        "interview.completed",
        locked,
        payload={
            "session_public_id": str(locked.public_id),
            "kind": locked.kind,
            "turns": 0,
            "practice": False,
        },
    )
    audit.record(verb="interview.completed", actor=actor, target=locked, metadata={"turns": 0})
    return locked


def upcoming_for(*, candidate: Any = None, membership: Any = None) -> QuerySet[InterviewSession]:
    """Interviews still to happen, for one side or the other."""
    queryset = InterviewSession.objects.filter(
        state=SessionState.SCHEDULED, scheduled_at__gte=timezone.now()
    ).select_related("application", "application__job", "candidate", "template")

    if candidate is not None:
        queryset = queryset.filter(candidate=candidate)
    if membership is not None:
        queryset = queryset.filter(participants__user=membership.user)
    return queryset.order_by("scheduled_at").distinct()


@transaction.atomic
def sweep_holds(*, limit: int = 500) -> int:
    """Return expired holds to the market.

    `open_slots` already ignores them, so this is tidying rather than
    correctness — but a table where half the rows say `held` and mean `open` is
    a table nobody can read, and the state that shows in the admin should be
    true.
    """
    stale = InterviewSlot.objects.filter(
        state=SlotState.HELD, held_until__lt=timezone.now()
    ).values_list("pk", flat=True)[:limit]

    return InterviewSlot.objects.filter(pk__in=list(stale)).update(
        state=SlotState.OPEN, held_until=None, updated_at=timezone.now()
    )


def availability_for(membership: CompanyMembership) -> QuerySet[InterviewerAvailability]:
    return InterviewerAvailability.objects.filter(
        membership=membership, ends_at__gte=timezone.now()
    ).order_by("starts_at")


def when_text(moment: dt.datetime | None) -> str:
    """A time as a person reads it in a notification.

    The payload carried `isoformat()`, so the bell said "booked for
    2026-09-15T04:15:00+00:00" — UTC, to a candidate in Kathmandu. Rendered in
    the site's zone and labelled with it, because a notification is read on a
    phone hours later with nothing else on screen to say which zone it means.
    """
    if moment is None:
        return ""
    local = timezone.localtime(moment)
    # `%-d` is glibc-only and raises on Windows; the day is formatted by hand.
    return f"{local:%A} {local.day} {local:%B, %H:%M} ({local.tzname()})"


# --- internals ------------------------------------------------------------


def _invite(session: InterviewSession, users: list[User]) -> None:
    from ..models import InterviewParticipant

    for user in users:
        InterviewParticipant.objects.get_or_create(
            session=session,
            user=user,
            defaults={"role": ParticipantRole.INTERVIEWER},
        )


def _release(session: InterviewSession) -> None:
    """Put a session's slot back on the market."""
    slot = getattr(session, "slot", None)
    if slot is None:
        return
    slot.state = SlotState.OPEN
    slot.session = None
    slot.held_until = None
    slot.save(update_fields=["state", "session", "held_until", "updated_at"])
