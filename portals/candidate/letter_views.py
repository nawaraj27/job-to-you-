"""
Cover letter screens (P14).

A library rather than a field on the apply form, which is what P8 shipped. A
letter is reused, edited between applications and kept for months; a textarea
on a page the candidate reaches once per job is where letters go to be lost.

**Drafting is a POST, always.** It calls a model and it creates a row; a GET
that a refresh repeats would spend somebody's budget twice for one click.

**Nothing here sends anything.** There is no path from this screen to an
application: applying is its own act, on its own screen (FR-6.1). What this
screen does is get words onto a page the candidate then owns.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.applications import selectors
from apps.applications.models import LetterTone
from apps.applications.services import letters as letter_service
from apps.candidates.services import profile as profile_service
from apps.common.errors import DomainError, NotFound
from apps.jobs import selectors as job_selectors


class CandidateLetterView(View):
    """Base: a signed-in candidate and their own letters."""

    @method_decorator(login_required)
    def dispatch(self, request: HttpRequest, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def profile(self, request: HttpRequest):
        # Created on first use, as every other candidate screen does. It answered
        # 404 for an account that had not opened the dashboard yet.
        return profile_service.get_or_create_profile(user=request.user)

    def letter(self, request: HttpRequest, public_id: str):
        """One of this candidate's letters, or a 404.

        `NotFound` becomes `Http404` rather than a 403: a 403 would confirm the
        letter exists, and what a named person wrote to a named employer is
        exactly what must not be confirmable by URL.
        """
        try:
            return letter_service.find(profile=self.profile(request), public_id=public_id)
        except NotFound as exc:
            raise Http404(exc.message) from exc


class LetterListView(CandidateLetterView):
    """`/letters/` — everything the candidate has written or drafted."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        return render(
            request,
            "letters/list.html",
            {
                "page_title": "Cover letters",
                "letters": selectors.cover_letters_for(profile).select_related("job", "ai_run"),
                "tones": LetterTone.choices,
                "limit": letter_service.MAX_LETTERS,
            },
        )


class LetterDetailView(CandidateLetterView):
    """`/letters/<id>/` — read and edit one letter."""

    def get(self, request: HttpRequest, public_id: str) -> HttpResponse:
        letter = self.letter(request, public_id)
        return render(request, "letters/detail.html", _context(letter))

    def post(self, request: HttpRequest, public_id: str) -> HttpResponse:
        letter = self.letter(request, public_id)
        message = error = None

        try:
            letter = letter_service.update(
                letter=letter,
                actor=request.user,
                content=request.POST.get("content", ""),
                title=request.POST.get("title"),
            )
            message = "Saved"
        except DomainError as exc:
            error = exc

        return _fragment(request, "letters/_editor.html", _context(letter), message, error)


class LetterCommandView(CandidateLetterView):
    """`/letters/act/` — create, draft, duplicate and delete.

    One endpoint because they are all "what happens to this letter next", and
    the service decides which answers are legal rather than the URL router.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        command = request.POST.get("command", "")

        try:
            if command == "draft":
                return self._draft(request, profile)
            if command == "create":
                letter = letter_service.save(
                    profile=profile,
                    actor=request.user,
                    content=request.POST.get("content", "") or "Dear hiring team,",
                    title=request.POST.get("title", ""),
                )
                return redirect("letters:detail", public_id=letter.public_id)
            if command == "duplicate":
                letter = letter_service.duplicate(
                    letter=self.letter(request, request.POST.get("letter", "")),
                    actor=request.user,
                )
                return redirect("letters:detail", public_id=letter.public_id)
            if command == "delete":
                letter_service.delete(
                    letter=self.letter(request, request.POST.get("letter", "")),
                    actor=request.user,
                )
                return redirect("letters:list")
        except DomainError as exc:
            return _fragment(
                request,
                "letters/_list_rows.html",
                {
                    "letters": selectors.cover_letters_for(profile).select_related("job"),
                    "limit": letter_service.MAX_LETTERS,
                },
                None,
                exc.message,
            )

        raise Http404

    def _draft(self, request: HttpRequest, profile) -> HttpResponse:
        """Ask a model for a first draft (SRS §9.4).

        A failed draft is not an error page. The candidate asked for help
        writing something; the honest response is an empty editor and a sentence
        saying the draft is unavailable, which is what they had before this
        feature existed (SRS §9.3.6).
        """
        job = job_selectors.published_job(request.POST.get("job", ""))
        if job is None:
            raise Http404("That job is not available.")

        from apps.billing.services.entitlements import PlanLimitExceeded

        try:
            letter, run = letter_service.draft(
                profile=profile,
                actor=request.user,
                job=job,
                tone=request.POST.get("tone", LetterTone.DIRECT),
                notes=request.POST.get("notes", ""),
            )
        except PlanLimitExceeded as limit:
            # A plan allowance is a normal state with a way on, not a failure.
            return _fragment(
                request,
                "letters/_draft_unavailable.html",
                {"job": job, "run": None, "write_url": reverse("letters:list")},
                None,
                limit,
            )

        if letter is None:
            return _fragment(
                request,
                "letters/_draft_unavailable.html",
                {"job": job, "run": run, "write_url": reverse("letters:list")},
                None,
                "A draft could not be written just now. You can write your own.",
            )

        return redirect("letters:detail", public_id=letter.public_id)


def _context(letter) -> dict[str, Any]:
    return {
        "page_title": letter.title or "Cover letter",
        "letter": letter,
        # A sent letter is part of what an employer read. The editor is
        # read-only rather than hidden, because the candidate still wants to see
        # what they sent (ADR-A8's reasoning, applied to a letter).
        "locked": letter.is_sent,
        "gaps": _gaps(letter),
        "cliches": letter_service.cliches_in(letter.content),
    }


def _gaps(letter) -> list[str]:
    """What the draft could not claim: the posting's skills minus theirs.

    Knowing which requirements the letter does not address is more useful than
    a letter that addresses them by inventing something. It is computed, not
    asked for — this used to print `ai_run.output["gaps"]`, and a model naming
    two skills the candidate had listed told her she was missing them on the
    same screen the matcher scored them as matched.
    """
    if letter.job_id is None:
        return []
    return letter_service.missing_skills(profile=letter.profile, job=letter.job)[:5]


def _fragment(request, template, context, message, error):
    from apps.common import fragments, restrictions

    # An ordinary form post (the list's forms are plain posts) is answered
    # with a page, not with this region on its own (`common.fragments`).
    page = fragments.page_answer(
        request, message=message, error=error, fallback_url=reverse("letters:list")
    )
    if page is not None:
        return page

    context["flash_message"] = message if not error else None
    # The refusal itself, not only its words: a letter the plan does not
    # cover is a restriction with a way on, not a red failure.
    context.update(restrictions.flash_context(request, error))
    return render(request, template, context)
