"""
Candidate application screens (SRS §11.2).

Three of them, and the third is the one that matters most:

  * **apply** — choose a CV version, answer the screening questions, send;
  * **the list** — what is out there, and what happened to it;
  * **one application** — the timeline, filtered to what the candidate may see,
    plus the CV they actually sent and the posting as it read on the day.

The last two are why `job_snapshot` and `submitted_render_sha256` exist: an
applicant who cannot see what they applied to, or which CV went, is being asked
to trust a record they have no access to.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.applications import policies, selectors
from apps.applications.services import apply as apply_service
from apps.applications.services import offers as offer_service
from apps.candidates.services import profile as profile_service
from apps.candidates.services import talent as talent_service
from apps.common import restrictions
from apps.common.errors import DomainError, NotFound
from apps.jobs import policies as job_policies
from apps.jobs import selectors as job_selectors
from apps.resumes import selectors as resume_selectors

from . import job_context as job_shape


def _profile(request: HttpRequest):
    # Created on first use, as every other candidate screen does. These four
    # answered 404 for an account that had not opened the dashboard yet —
    # an employer clicking "Saved jobs" in their own account menu, say.
    return profile_service.get_or_create_profile(user=request.user)


@method_decorator(login_required, name="dispatch")
class ApplyView(View):
    """`/jobs/<slug>/apply/` — the form, then the submission."""

    def get(self, request: HttpRequest, slug: str) -> HttpResponse:
        profile = _profile(request)
        job = self._job(slug)

        return render(request, "jobs/apply.html", self._context(request, profile, job))

    def post(self, request: HttpRequest, slug: str) -> HttpResponse:
        profile = _profile(request)
        job = self._job(slug)

        version = self._version(request, profile)
        try:
            application = apply_service.apply_to_job(
                job=job,
                profile=profile,
                actor=request.user,
                resume_version=version,
                cover_letter=self._cover_letter(request, profile, job),
                cover_letter_text=request.POST.get("cover_letter_text", ""),
                answers=self._answers(request, job),
            )
        except DomainError as exc:
            restrictions.flash(request, exc)
            return render(
                request,
                "jobs/apply.html",
                {
                    **self._context(request, profile, job),
                    "error": exc.message,
                    # What they chose and typed comes back with the error. A
                    # refusal used to return a blank form, so a candidate who
                    # had written a letter lost it to a missing answer.
                    "posted": {
                        "document": request.POST.get("document", ""),
                        "cover_letter": request.POST.get("cover_letter", ""),
                        "cover_letter_text": request.POST.get("cover_letter_text", ""),
                        "answers": {
                            key.removeprefix("question_"): request.POST.getlist(key)
                            for key in request.POST
                            if key.startswith("question_")
                        },
                    },
                },
                status=400,
            )

        messages.success(request, f"Applied to {job.title}.")
        return redirect("applications:detail", public_id=application.public_id)

    def _job(self, slug: str):
        try:
            return job_policies.require_visible(job_selectors.published_job(slug))
        except NotFound as exc:
            raise Http404(exc.message) from exc

    def _context(self, request: HttpRequest, profile, job) -> dict[str, Any]:
        return {
            "job": job_shape.job_detail(job),
            "raw_job": job,
            "questions": job.questions.all(),
            "documents": resume_selectors.documents_to_apply_with(profile),
            "letters": selectors.cover_letters_for(profile),
            "existing": selectors.applications_of(profile).filter(job=job).first(),
        }

    def _version(self, request: HttpRequest, profile):
        """The CV that will be frozen onto the application.

        A document is chosen on screen; the version is created from it here, so
        what is sent is the CV as it reads at this moment and not whatever it
        becomes next week (ADR-A19).
        """
        from apps.resumes.models import VersionReason
        from apps.resumes.services import versions as version_service

        document = resume_selectors.document_for(profile, request.POST.get("document", ""))
        if document is None:
            raise Http404("Choose a CV to send.")
        return version_service.finalize(
            document=document, actor=request.user, reason=VersionReason.APPLIED
        )

    def _cover_letter(self, request: HttpRequest, profile, job):
        """A saved letter the candidate chose, if any.

        Looked up by `public_id` within this candidate's own letters (P14): a
        primary key in a form is a key somebody can walk. A letter typed on
        this screen is not saved here — the apply service saves it once the
        application is certain to be created.
        """
        chosen = request.POST.get("cover_letter")
        if chosen:
            return selectors.cover_letters_for(profile).filter(public_id=chosen).first()
        return None

    def _answers(self, request: HttpRequest, job) -> dict[str, Any]:
        from apps.jobs.models import QuestionKind

        answers: dict[str, Any] = {}
        for question in job.questions.all():
            field = f"question_{question.pk}"
            if question.kind == QuestionKind.MULTI_CHOICE:
                answers[str(question.pk)] = request.POST.getlist(field)
            else:
                answers[str(question.pk)] = request.POST.get(field, "")
        return answers


@method_decorator(login_required, name="dispatch")
class ApplicationListView(View):
    """`/applications/` — one pipeline, from applied to offer."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _profile(request)
        applications = selectors.applications_of(profile)

        live = [row for row in applications if row.is_live]
        closed = [row for row in applications if not row.is_live]

        return render(
            request,
            "applications/list.html",
            {
                "page_title": "Applications",
                "live": live,
                "closed": closed,
                "total": len(live) + len(closed),
            },
        )


def _interview_context(application) -> dict:
    """The interview, or the invitation to book one.

    The booking screen existed and nothing linked to it. An employer published
    times, the candidate was moved to the interview stage, and then the two of
    them sat waiting for each other — the whole scheduling loop was reachable
    only by typing the URL.

    So the application says where it stands: a booked interview with the time
    on it, or an offer to choose one when the employer has opened times and the
    application has reached the stage where that is the next thing to do.
    """
    from apps.core.features import is_enabled
    from apps.interviews.models import SessionState
    from apps.interviews.services import scheduling

    if not is_enabled("interview"):
        return {"interview": None, "can_book": False, "open_slot_count": 0, "awaiting_times": False}

    booked = (
        application.interview_sessions.filter(
            state__in=(SessionState.SCHEDULED, SessionState.IN_PROGRESS)
        )
        .select_related("template")
        .order_by("scheduled_at")
        .first()
    )
    if booked is not None:
        return {
            "interview": booked,
            "can_book": False,
            "open_slot_count": 0,
            "awaiting_times": False,
        }

    if not application.is_live:
        return {"interview": None, "can_book": False, "open_slot_count": 0, "awaiting_times": False}

    open_slots = scheduling.open_slots(company=application.job.company).count()
    reached = _reached_interview(application)
    return {
        "interview": None,
        # Offered from the interview stage onward. Earlier than that, a
        # candidate booking a time is booking a conversation nobody at the
        # company has decided to have.
        "can_book": open_slots > 0 and reached,
        "open_slot_count": open_slots,
        # Reached the interview stage, and there is nothing to book. The screen
        # used to show nothing at all here: somebody told they were at
        # interview stage, with no time, no explanation and no next step,
        # deciding for themselves whether to chase it.
        "awaiting_times": reached and open_slots == 0,
    }


#: The stage from which booking an interview makes sense.
INTERVIEW_STAGE_ORDER = 40


def _reached_interview(application) -> bool:
    stage = application.stage
    return bool(stage and stage.order >= INTERVIEW_STAGE_ORDER and not stage.is_terminal)


@method_decorator(login_required, name="dispatch")
class ApplicationDetailView(View):
    """`/applications/<public_id>/` — one application, from the inside."""

    def get(self, request: HttpRequest, public_id: str) -> HttpResponse:
        profile = _profile(request)
        application = selectors.application_of(profile, public_id)
        if application is None:
            raise Http404("No such application.")

        return render(
            request,
            "applications/detail.html",
            {
                "page_title": application.job.title,
                "application": application,
                # The frozen posting, not the live one: this is what they
                # applied to, whatever the employer has edited since.
                "snapshot": application.job_snapshot,
                "snapshot_salary": job_shape.snapshot_salary(application.job_snapshot),
                "timeline": selectors.candidate_timeline(application),
                "offer": (offer := selectors.live_offer(application)),
                # Only once sent: the grant that lets the candidate read the
                # letter is written at send, and a draft is not theirs yet.
                "offer_document_url": (
                    offer_service.document_url(offer, request.user)
                    if offer is not None and offer.state == "sent"
                    else ""
                ),
                "can_withdraw": application.is_live,
                **_interview_context(application),
            },
        )

    def post(self, request: HttpRequest, public_id: str) -> HttpResponse:
        profile = _profile(request)
        application = selectors.application_of(profile, public_id)
        if application is None:
            raise Http404("No such application.")

        back = reverse("applications:detail", args=[public_id])
        action = request.POST.get("action")

        try:
            if action == "withdraw":
                policies.require_owner(application, profile)
                apply_service.withdraw(
                    application=application,
                    actor=request.user,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(request, "Withdrawn. The employer has been told.")
            elif action in {"accept_offer", "decline_offer"}:
                offer = selectors.live_offer(application)
                if offer is None:
                    raise NotFound("There is no offer to answer.")
                offer_service.respond(
                    offer=offer,
                    actor=request.user,
                    accept=action == "accept_offer",
                    reason=request.POST.get("reason", ""),
                )
                messages.success(
                    request,
                    "Offer accepted." if action == "accept_offer" else "Offer declined.",
                )
            else:
                raise Http404("Unknown action.")
        except DomainError as exc:
            restrictions.flash(request, exc)
            return redirect(back)

        return redirect(back)


@method_decorator(login_required, name="dispatch")
class VisibilityView(View):
    """`/applications/visibility/` — who can see me, and why (INV-4).

    A privacy screen that answers the question with rows rather than with
    reassurance. Every line is a `CandidateVisibilityGrant`, and every one names
    the reason the company has access.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _profile(request)
        return render(
            request,
            "applications/visibility.html",
            {
                "page_title": "Who can see you",
                "grants": selectors.grants_for(profile),
                # FR-2.8, added in P15. Access and *use* of access are different
                # facts: a grant says a company may look, and this says they
                # did. A screen that showed only the first would answer the
                # question nobody asks.
                "views": talent_service.views_for(profile),
            },
        )
