"""
Employer interview screens (FR-10.6 to FR-10.9, FR-10.11).

Three screens: the calendar an interviewer publishes, the list of interviews,
and one interview with its scorecard.

**A scorecard is written and submitted in one act**, and until it is submitted
nobody else can see it (FR-10.8). Anchoring is the most studied failure in
interview panels; the screen never shows another interviewer's score to somebody
who has not written their own, because `visible_evaluations` does not return it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View

from apps.common import restrictions
from apps.common.errors import DomainError, InvalidState, NotFound
from apps.common.ids import as_pk
from apps.companies import policies as company_policies
from apps.core.features import is_enabled
from apps.interviews import policies, selectors
from apps.interviews.models import InterviewKind, ParticipantRole, SessionState
from apps.interviews.services import evaluation, rooms, scheduling


class EmployerInterviewView(View):
    """Base: a member of this company, and the flag on."""

    @method_decorator(login_required)
    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if not is_enabled("interview"):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def session(self, request: HttpRequest, public_id: str):
        membership = company_policies.require_membership(request.membership)
        found = selectors.session_for_company(request.company, public_id)
        if found is None:
            raise Http404("That interview is not available.")
        try:
            policies.require_employer(found, membership)
        except NotFound as exc:
            # §11.4: access runs from scheduling until thirty days after. An
            # interviewer who can read an application forever because they once
            # had a call is a permission nobody granted.
            raise Http404(exc.message) from exc
        return found


class InterviewListView(EmployerInterviewView):
    """`/employer/<slug>/interviews/` — what is booked and what has happened."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        from apps.applications.models import Application

        # The same reach as the applicant board: a Hiring Manager's own jobs,
        # an Interviewer's own panels (SRS §2.3).
        sessions = selectors.sessions_for_company(request.company).filter(
            application__in=Application.objects.visible_to(membership)
        )

        return render(
            request,
            "employer/interviews/list.html",
            {
                "company": request.company,
                "membership": membership,
                "upcoming": sessions.filter(
                    state__in=(SessionState.SCHEDULED, SessionState.IN_PROGRESS),
                    scheduled_at__gte=timezone.now() - dt.timedelta(hours=3),
                ),
                "completed": sessions.filter(state=SessionState.COMPLETED)[:20],
                "slots": selectors.slots_for(membership)[:40],
                # A Viewer reads interviews and publishes nobody's calendar.
                "can_publish_slots": membership.role != "viewer",
                # Published times are read in the server's zone. Saying which
                # one stops an interviewer abroad booking every slot an hour out.
                "time_zone": timezone.get_current_timezone_name(),
            },
        )


class InterviewSlotsView(EmployerInterviewView):
    """`/employer/<slug>/interviews/slots/` — publish times (FR-10.6)."""

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        back = reverse("employer:interviews", args=[company_slug])

        try:
            from apps.companies.permissions import Role

            if membership.role == Role.VIEWER:
                # Read-only in every column of §2.3; a published time is a
                # promise of somebody's calendar.
                raise InvalidState("Your role can read interviews but not publish times.")
            windows = _windows(request.POST)
            scheduling.publish_slots(membership=membership, actor=request.user, windows=windows)
            messages.success(
                request, f"Published {len(windows)} time{'s' if len(windows) != 1 else ''}."
            )
        except DomainError as exc:
            restrictions.flash(request, exc)

        return redirect(back)


class InterviewDetailView(EmployerInterviewView):
    """`/employer/<slug>/interviews/<id>/` — one interview and its scorecard."""

    def get(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        session = self.session(request, public_id)
        from apps.applications import policies as application_policies

        # A Viewer reads lists, not candidates (SRS §2.3); the interview is the
        # candidate's answers.
        application_policies.require_can_open_detail(membership)
        on_panel = session.participants.filter(user=request.user).exists()

        return render(
            request,
            "employer/interviews/detail.html",
            {
                "company": request.company,
                "membership": membership,
                "session": session,
                "turns": selectors.turns_for(session),
                # FR-10.8: submitted rows, plus this viewer's own.
                "evaluations": evaluation.visible_evaluations(session=session, viewer=request.user),
                "outcome": getattr(session, "outcome", None),
                "competencies": _competency_choices(session),
                "has_started": (
                    session.scheduled_at is not None and session.scheduled_at <= timezone.now()
                ),
                "mine": {
                    row.competency: row
                    for row in session.evaluations.filter(evaluator=request.user)
                },
                "can_join": policies.can_join(session),
                "can_score": on_panel
                or application_policies.can_write(
                    session.application, membership, application_policies.WRITE_COMMENT
                ),
                "can_manage": application_policies.can_write(session.application, membership),
                # Times this interview could move to. Only shown while it is
                # still ahead — moving an interview that has happened is not a
                # thing anybody means to do.
                "open_slots": (
                    scheduling.open_slots(company=request.company)[:40]
                    if not session.is_finished
                    else []
                ),
            },
        )

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        # `self.session` requires membership and the access window itself, so
        # the check is not repeated here — one call, one place it can be wrong.
        session = self.session(request, public_id)
        back = reverse("employer:interview_detail", args=[company_slug, public_id])
        action = request.POST.get("action", "score")
        membership = company_policies.require_membership(request.membership)

        try:
            from apps.applications import policies as application_policies

            application_policies.require_can_open_detail(membership)
            if action == "score":
                # A panel member scores; so may somebody who could comment on
                # the application anyway. A Viewer or an unassigned member may
                # not put a score on a candidate they were never asked about.
                if not session.participants.filter(user=request.user).exists():
                    application_policies.require_can_write(
                        session.application, membership, application_policies.WRITE_COMMENT
                    )
            else:
                application_policies.require_can_write(session.application, membership)

            if action == "score":
                evaluation.submit_scorecard(
                    session=session,
                    evaluator=request.user,
                    competency=request.POST.get("competency", ""),
                    score=_as_int(request.POST.get("score")),
                    rationale=request.POST.get("rationale", ""),
                )
                messages.success(request, "Scorecard submitted.")

            elif action == "conclude":
                if session.kind == InterviewKind.LIVE:
                    # Recording the outcome is the moment a live interview is
                    # over. Nothing else ever moved it out of "scheduled".
                    scheduling.complete_live(session=session, actor=request.user)
                evaluation.conclude(session=session, actor=request.user)
                messages.success(request, "Outcome recorded.")

            elif action == "share":
                evaluation.share_with_candidate(session=session, actor=request.user)
                messages.success(request, "Feedback shared with the candidate.")

            elif action == "reschedule":
                # `scheduling.reschedule` existed from the start and nothing
                # called it: an employer whose interviewer went sick could
                # cancel the interview, which drops the candidate back to
                # nothing, but could not move it.
                slot = (
                    scheduling.open_slots(company=request.company)
                    .filter(pk=as_pk(request.POST.get("slot")))
                    .first()
                )
                if slot is None:
                    raise InvalidState("That time is no longer open.")

                moved = scheduling.reschedule(
                    session=session,
                    slot=slot,
                    actor=request.user,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(
                    request, f"Moved to {moved.scheduled_at:%d %b, %H:%M}. The candidate is told."
                )
                return redirect(
                    reverse("employer:interview_detail", args=[company_slug, str(moved.public_id)])
                )

            elif action == "cancel":
                scheduling.cancel(
                    session=session,
                    actor=request.user,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(request, "Interview cancelled.")

            elif action == "no_show":
                scheduling.mark_no_show(
                    session=session,
                    actor=request.user,
                    missing_side=request.POST.get("missing_side", "candidate"),
                )
                messages.success(request, "Recorded as a no-show.")

            else:
                raise InvalidState("That is not something you can do to an interview.")

        except DomainError as exc:
            restrictions.flash(request, exc)

        return redirect(back)


class InterviewJoinView(EmployerInterviewView):
    """`/employer/<slug>/interviews/<id>/join/` — into the room, as the host.

    The detail page linked "Join the room" to `session.room_ref`, which is a
    room *name* rather than a URL: the browser resolved it relative to the page
    and every interviewer who clicked it landed on a 404. Joining goes through
    here so the provider builds the URL and the attendance is recorded.
    """

    def get(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        session = self.session(request, public_id)
        back = reverse("employer:interview_detail", args=[company_slug, public_id])

        # The room is the panel's, not the company's. Any member can read the
        # interview; only somebody invited onto it goes into the room, and only
        # an interviewer goes in as the moderator.
        participant = session.participants.filter(user=request.user).first()
        if participant is None:
            messages.error(
                request,
                "You are not on this interview's panel. Ask whoever booked it to add you.",
            )
            return redirect(back)

        try:
            session = scheduling.record_join(session=session, user=request.user)
            url = rooms.join_url(
                session=session,
                user=request.user,
                is_host=participant.role == ParticipantRole.INTERVIEWER,
            )
        except DomainError as exc:
            restrictions.flash(request, exc)
            return redirect(back)

        return redirect(url)


# --- helpers --------------------------------------------------------------


def _competency_choices(session) -> list[tuple[str, str]]:
    """Scorecard competencies as (key, label), so the form shows words, not slugs."""
    rows = session.plan.get("competencies") or [
        {"key": key, **(pool if isinstance(pool, dict) else {})}
        for key, pool in sorted((session.template.question_pools or {}).items())
    ]
    return [
        (
            row["key"],
            row.get("label") or row["key"].replace("_", " ").capitalize(),
        )
        for row in rows
        if row.get("key")
    ]


def _windows(data: Any) -> list[tuple[dt.datetime, dt.datetime]]:
    """Parse the published windows from the form.

    One date plus a list of times, because that is how somebody thinks about
    their calendar — "Tuesday, and these four slots" rather than eight separate
    datetime pairs.
    """
    from django.utils.dateparse import parse_date, parse_time

    day = parse_date(data.get("date", "") or "")
    if day is None:
        raise InvalidState("Pick a date.")

    times = [value for value in data.getlist("time") if value]
    if not times:
        raise InvalidState("Pick at least one time.")

    minutes = _as_int(data.get("minutes")) or scheduling.DEFAULT_SLOT_MINUTES
    windows = []
    for value in times:
        clock = parse_time(value)
        if clock is None:
            continue
        starts = timezone.make_aware(dt.datetime.combine(day, clock))
        windows.append((starts, starts + dt.timedelta(minutes=minutes)))

    if not windows:
        raise InvalidState("Those times could not be read.")
    return windows


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
