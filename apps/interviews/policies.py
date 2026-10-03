"""
Who may see and do what with an interview (SRS §11.4, INV-4).

The rule that shapes this module: **practice belongs to the candidate and
nobody else.** A practice session has no application, so there is no company to
authorise, and every employer-side check here refuses one outright rather than
walking a chain of relations that ends in `None`.

The second rule is time-bounded access. An interviewer's reach into an
application runs from scheduling until thirty days after the interview and then
stops. An interviewer who can read a candidate's application forever because
they once had a call with them is a permission nobody granted.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.common.errors import InvalidState, NotFound

from .models import InterviewSession, SessionState

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import CompanyMembership

#: How long after an interview an interviewer keeps access (§11.4).
ACCESS_DAYS_AFTER = 30


def require_candidate(
    session: InterviewSession, profile: CandidateProfile | None
) -> InterviewSession:
    """The candidate's own session, or `NotFound`.

    Never `NotAllowed`: a 403 on somebody else's interview confirms that the
    interview exists, which is a fact about a stranger's job search.
    """
    if profile is None or session.candidate_id != profile.pk:
        raise NotFound("That interview is not yours.")
    return session


def require_employer(
    session: InterviewSession, membership: CompanyMembership | None
) -> InterviewSession:
    """An employer's access to a session (§11.4).

    Refuses practice outright — there is no employer side to a practice session,
    and a company that could read one would be reading a candidate's private
    rehearsal.
    """
    if membership is None:
        raise NotFound("That interview is not available.")
    application = session.application
    if application is None:
        raise NotFound("That interview is not available.")
    if application.job.company_id != membership.company_id:
        raise NotFound("That interview is not available.")
    from apps.companies.permissions import Role

    if membership.role == Role.INTERVIEWER:
        # The thirty-day window is the Interviewer's, and only theirs (SRS
        # §2.3). It used to apply to every member, so an owner lost the
        # scorecards from a hire a month after the interview.
        if not _within_access_window(session):
            raise NotFound("That interview is no longer available.")
        if not session.participants.filter(user_id=membership.user_id).exists():
            raise NotFound("That interview is not available.")
        return session

    from apps.applications import policies as application_policies

    if not application_policies.can_read(application, membership):
        raise NotFound("That interview is not available.")
    return session


#: How long a live interview is assumed to last when it has no slot to say.
DEFAULT_LIVE_MINUTES = 60

#: How early the room opens, and how long after the end it stays open.
JOIN_MARGIN = dt.timedelta(minutes=15)


def join_window(session: InterviewSession) -> tuple[dt.datetime, dt.datetime] | None:
    """When the room is open: fifteen minutes before, until fifteen after the end.

    It used to close fifteen minutes after the *start*. A candidate whose
    connection dropped twenty minutes into a forty-five minute interview came
    back to "the room opens fifteen minutes before the interview" and could not
    get back in.
    """
    if session.scheduled_at is None:
        return None
    slot = getattr(session, "slot", None)
    ends_at = (
        slot.ends_at
        if slot is not None
        else session.scheduled_at + dt.timedelta(minutes=DEFAULT_LIVE_MINUTES)
    )
    return session.scheduled_at - JOIN_MARGIN, ends_at + JOIN_MARGIN


def can_join(session: InterviewSession) -> bool:
    """Whether the room is open to somebody entering it now — or entering again."""
    if session.state not in {SessionState.SCHEDULED, SessionState.IN_PROGRESS}:
        return False
    window = join_window(session)
    if window is None:
        return False
    return window[0] <= timezone.now() <= window[1]


def require_joinable(session: InterviewSession) -> InterviewSession:
    """`can_join`, with a reason a person can act on when it is false."""
    if can_join(session):
        return session
    if session.state == SessionState.CANCELLED:
        raise InvalidState("This interview was cancelled, so there is no room to join.")
    if session.state in SessionState.terminal():
        raise InvalidState("This interview has finished and its room is closed.")
    window = join_window(session)
    if window is not None and timezone.now() > window[1]:
        raise InvalidState("The time for this interview has passed and its room is closed.")
    raise InvalidState("The room opens fifteen minutes before the interview.")


def require_answerable(session: InterviewSession) -> InterviewSession:
    if session.is_finished:
        raise InvalidState("This interview has already finished.")
    return session


def _within_access_window(session: InterviewSession) -> bool:
    """Whether an interviewer's access has expired (§11.4).

    Measured from the end of the interview, or from its scheduled time when it
    never happened — a cancelled interview should not keep a company's access
    open indefinitely because it has no end date.
    """
    reference = session.ended_at or session.scheduled_at or session.created_at
    if reference is None:
        return True
    return timezone.now() <= reference + dt.timedelta(days=ACCESS_DAYS_AFTER)
