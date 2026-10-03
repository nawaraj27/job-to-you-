"""
Resume screens on real records (P5).

Replaces `apps/resume`, whose views read session-backed stand-ins. The URL
namespace stays `resume` because a dozen templates outside this portal link to
`resume:builder`, `resume:upload` and `resume:templates`, and renaming routes is
not what this phase is for.

Views parse input, call a policy, call one service or selector, and choose a
template. The three builder modes are three screens over one draft — the domain
does not care which one is open, and neither do these views beyond picking a
template.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.billing import limits
from apps.billing.services import entitlements
from apps.candidates import selectors as candidate_selectors
from apps.candidates.services import profile as profile_service
from apps.common import fragments
from apps.common.errors import DomainError, InvalidState
from apps.files import signing as file_signing
from apps.files.services import FileTooLarge, UnsupportedFileType
from apps.resumes import content as content_rules
from apps.resumes import (
    example_personas,
    examples,
    improvement,
    policies,
    presentation,
    readiness,
    selectors,
)
from apps.resumes.models import (
    BuilderMode,
    ImprovementJob,
    ImprovementJobKind,
    RenderFormat,
    TemplateTier,
    VersionReason,
)
from apps.resumes.services import analysis as analysis_service
from apps.resumes.services import assistant as assistant_service
from apps.resumes.services import builders as builder_service
from apps.resumes.services import claim as claim_service
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import improvements as improvement_service
from apps.resumes.services import proposals as proposal_service
from apps.resumes.services import reconcile as reconcile_service
from apps.resumes.services import renders as render_service
from apps.resumes.services import suggestions as suggestion_service
from apps.resumes.services import upload as upload_service
from apps.resumes.services import versions as version_service

from . import gallery

#: Where an anonymous visitor's upload tokens are kept until they register.
#: The session is the right place: it belongs to the browser, not to an account
#: that does not exist yet, and it disappears with the browser like the token's
#: usefulness does.
GUEST_TOKENS_KEY = "resume:guest_tokens"


logger = logging.getLogger(__name__)


class CandidateResumeView(View):
    """Base: a signed-in candidate, their profile, and their documents."""

    @method_decorator(login_required)
    def dispatch(self, request: HttpRequest, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def profile(self, request: HttpRequest):
        profile = profile_service.get_or_create_profile(user=request.user)
        # Handed to the shell so the greeting and the rail do not each look up
        # the same row again while rendering this page.
        candidate_selectors.remember_profile(request, profile)
        return profile

    def document(self, request: HttpRequest, public_id: str | None = None):
        """The named document, or the one the candidate was last working on."""
        profile = self.profile(request)

        if public_id:
            document = selectors.document_for(profile, public_id)
        else:
            document = (
                selectors.primary_document_for(profile)
                or selectors.active_documents_for(profile).first()
            )

        if document is None:
            return None
        return policies.require_document_owner(document, profile)


def _fragment(
    request, template, context, *, message=None, error=None, fallback_url="", next_url=""
):
    """Re-render a region with a confirmation or a refusal.

    `error` may be the `DomainError` itself rather than its message, so an
    expected refusal — a plan limit above all — renders as a restriction with
    its next step instead of as a red failure (`common.restrictions`).

    A full-page form that posts here gets a page, not the region
    (`common.fragments`): the region alone has no stylesheet and no shell.
    """
    from apps.common import fragments, restrictions

    page = fragments.page_answer(
        request,
        message=message,
        error=error,
        fallback_url=fallback_url or reverse("resume:builder"),
        next_url=next_url,
    )
    if page is not None:
        return page

    context.update(restrictions.flash_context(request, error))
    context["flash_message"] = message if not error else None
    restriction = context.get("flash_restriction")
    # A refusal keeps its real status (RES-03): a free candidate's spent
    # analysis came back 200 here, while the same refusal through the
    # middleware was a 402. `X-Restriction` is what lets htmx swap a 4xx.
    response = render(request, template, context, status=restriction.status if restriction else 200)
    if restriction:
        response["X-Restriction"] = restriction.kind
    announce = (getattr(error, "message", None) or error) or message
    if announce:
        response["HX-Trigger"] = json.dumps({"builder:announce": announce})
    return response


# --- upload ---------------------------------------------------------------


class UploadView(CandidateResumeView):
    """`/cv/upload/` — the front door for a CV that already exists."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        if not request.headers.get("HX-Request"):
            # A visit, not this page's own poll: the poll landing after the
            # read finished used to retire the chip before anyone had looked.
            _forget_finished(request, profile)
        return render(
            request,
            "resume/upload.html",
            {
                "page_title": "Upload your CV",
                "uploads": selectors.uploads_for(profile),
                "proposal_groups": selectors.grouped_proposals(profile),
                # This screen is in the rail, so it is where somebody comes
                # back to after leaving a CV to be read. While anything is
                # still moving it refreshes itself; otherwise a candidate who
                # returns sees "Waiting to be parsed" for ever and concludes
                # it is stuck.
                "waiting": selectors.uploads_in_flight(profile).exists(),
            },
        )


class UploadReceiveView(CandidateResumeView):
    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        uploaded = request.FILES.get("file")

        if uploaded is None:
            return _fragment(
                request,
                "resume/_upload_error.html",
                {"error": "Choose a file first."},
                error="Choose a file first.",
            )

        try:
            upload, created = upload_service.upload_for_profile(
                profile=profile, actor=request.user, uploaded_file=uploaded
            )
        except (UnsupportedFileType, FileTooLarge, DomainError) as exc:
            title, message = _upload_refusal(exc)
            return _fragment(
                request,
                "resume/_upload_error.html",
                {"title": title, "error": message},
                error=f"{title} {message}",
            )

        from apps.resumes import progress

        progress.watch(request, str(upload.public_id))
        return _fragment(
            request,
            "resume/_upload_accepted.html",
            {"upload": upload, **_history_context(profile)},
            message="Uploaded" if created else "You already had that CV",
        )


def _upload_refusal(exc: Exception) -> tuple[str, str]:
    """(title, reason) for a refused CV: what to send, then what was wrong."""
    from apps.resumes.services.validation import HEADLINE

    message = str(getattr(exc, "message", "") or exc)
    if message.startswith(HEADLINE):
        return HEADLINE, message[len(HEADLINE) :].strip()
    return "That file was not accepted", message


class UploadDeleteView(CandidateResumeView):
    def post(self, request: HttpRequest, upload_id: str) -> HttpResponse:
        profile = self.profile(request)
        upload = selectors.upload_for(profile, upload_id)
        if upload is None:
            raise Http404

        upload_service.delete_upload(upload=upload, actor=request.user)
        return _fragment(
            request,
            "resume/_upload_history.html",
            _history_context(profile),
            message="Removed",
        )


def _gaps_required_first(target: dict[str, Any]) -> list[str]:
    """A target's missing skills, the required ones asked about first.

    The assistant opened a UI/UX conversation with "Design systems" — a
    preferred skill that happens to sort first — while three required ones
    waited behind it.
    """
    missing = list(target.get("missing") or [])
    required = [name for name in target.get("missing_required", missing) if name in missing]
    return required + [name for name in missing if name not in required]


def _forget_finished(request: HttpRequest, profile) -> None:
    """The results are on this screen now, so the top bar stops pointing here."""
    from apps.resumes import progress

    watched = request.session.get(progress.SESSION_KEY)
    if not watched:
        return
    moving = {
        str(value)
        for value in selectors.uploads_in_flight(profile).values_list("public_id", flat=True)
    }
    progress.forget(request, [value for value in watched if value not in moving])


class ProgressChipView(CandidateResumeView):
    """`/cv/upload/progress-chip/` — the top-bar chip, asked for again (CVP-04).

    Polled by the chip only while a CV is moving; the answer without a poll
    attribute is what stops it.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        from apps.resumes import progress

        return render(
            request, "resume/_progress_chip.html", {"cv_progress": progress.current(request)}
        )


def _history_context(profile) -> dict[str, Any]:
    """What `_upload_history.html` needs, wherever it is rendered from.

    `waiting` is what keeps the list refreshing while a file is still being
    scanned or read (UPL-01). The upload and delete answers used to leave it
    out, so the list they drew never refreshed.
    """
    return {
        "uploads": selectors.uploads_for(profile),
        "waiting": selectors.uploads_in_flight(profile).exists(),
    }


class ProcessingView(CandidateResumeView):
    """Where an upload waits for the scanner and the reader."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        return render(
            request,
            "resume/processing.html",
            {
                "page_title": "Reading your CV",
                "uploads": selectors.uploads_for(profile),
                # Whether anything is still moving, so the page polls while it
                # is and stops when it is not. A screen that refreshes itself
                # for ever also reads as a screen you have to stay on.
                "waiting": selectors.uploads_in_flight(profile).exists(),
                "back_url": _back_url(request),
            },
        )


def _back_url(request: HttpRequest) -> str:
    """Where Close and Minimize return to: the page the candidate came from."""
    from django.utils.http import url_has_allowed_host_and_scheme

    referer = request.headers.get("referer") or ""
    if referer and url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        from urllib.parse import urlparse

        path = urlparse(referer).path
        if path != reverse("resume:processing"):
            return path
    return reverse("dashboard:index")


class ImportView(CandidateResumeView):
    """Accept or reject what a parse proposed, or turn it into a document."""

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        command = request.POST.get("command", "")
        message = error = None

        try:
            if command == "accept":
                proposal = selectors.proposal_for(profile, int(request.POST.get("id", 0)))
                if proposal is None:
                    raise Http404
                proposal_service.accept(proposal=proposal, actor=request.user)
                message = "Added to your profile"

            elif command == "reject":
                proposal = selectors.proposal_for(profile, int(request.POST.get("id", 0)))
                if proposal is None:
                    raise Http404
                proposal_service.reject(proposal=proposal, actor=request.user)
                message = "Dismissed"

            elif command == "accept_all":
                # Per kind when one is named. The service has taken a `kind`
                # since it was written and nothing ever passed one, so the only
                # bulk action on the screen was all twenty-four decisions at
                # once — which is not a decision anybody makes.
                kind = request.POST.get("kind") or None
                result = proposal_service.accept_all(profile=profile, actor=request.user, kind=kind)
                message = f"Added {result['applied']} to your profile"
                if result["failed"]:
                    message += f", {result['failed']} could not be added"

            elif command == "build":
                upload = selectors.upload_for(profile, request.POST.get("upload", ""))
                if upload is None:
                    raise Http404
                document = document_service.create_from_upload(upload=upload, actor=request.user)
                return redirect("resume:editor", document_id=str(document.public_id))
            else:
                return HttpResponse("Unknown command", status=400)
        except (DomainError, ValueError) as exc:
            error = getattr(exc, "message", "That could not be done.")

        return _fragment(
            request,
            "resume/_import_proposals.html",
            {"proposal_groups": selectors.grouped_proposals(profile)},
            message=message,
            error=error,
        )


# --- builders -------------------------------------------------------------


class BuilderHubView(CandidateResumeView):
    """`/cv/builder/` — every CV this candidate has, and the way into a new one."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        return render(
            request,
            "resume/builder.html",
            {
                "page_title": "Your CVs",
                **_document_list_context(profile),
                "templates": selectors.active_templates(),
                # The assistant first: it is the main way to build a CV here.
                "modes": sorted(BuilderMode.choices, key=lambda c: c[0] != BuilderMode.ASSISTANT),
                # Arriving from a template card: that template is preselected,
                # and the worked example is the default, because somebody who
                # came here from a picture of a filled-in CV asked for one.
                "chosen_template": request.GET.get("template", ""),
                "start_from_example": request.GET.get("start") == "example",
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Start a CV. The only place a document is created from nothing.

        `start=example` fills the new draft with the worked example the gallery
        shows, because "what goes in this field" is a question a blank form
        cannot answer. It is a copy, made once, on an explicit request, and the
        candidate owns and overwrites it like anything else they typed — the
        example itself is a module constant and is never written to.
        """
        profile = self.profile(request)
        from_example = request.POST.get("start") == "example"
        try:
            document = document_service.create_document(
                profile=profile,
                actor=request.user,
                name=request.POST.get("name", ""),
                mode=request.POST.get("mode", BuilderMode.EDITOR),
                template=selectors.template_by_slug(request.POST.get("template", "")),
                content=(
                    examples.example_content(
                        name=profile.user.full_name,
                        email=profile.user.email,
                        template_slug=request.POST.get("template", ""),
                    )
                    if from_example
                    else None
                ),
            )
        except DomainError as exc:
            return _fragment(
                request,
                "resume/_document_list.html",
                _document_list_context(profile),
                error=exc,
                fallback_url=reverse("resume:builder"),
            )

        # The document-scoped route for each mode. `resume:ai` and
        # `resume:guided_default` take no document id and cannot be reversed
        # with one: starting a CV in "Describe your work" raised
        # NoReverseMatch and the candidate got a 500 instead of the
        # assistant. Prompt-based creation is one of the three ways in, and
        # the only one that could not be entered from the hub.
        route = {
            BuilderMode.GUIDED: "resume:guided",
            BuilderMode.ASSISTANT: "resume:assistant",
        }.get(document.builder_mode, "resume:editor")
        url = reverse(route, kwargs={"document_id": str(document.public_id)})
        # The builder says so on arrival rather than leaving somebody to
        # wonder whose history they are reading. A query parameter rather
        # than a flag on the draft: it is true of this page load, not of the
        # document, and a document is example-derived only until the first
        # field is replaced.
        return redirect(f"{url}?started=example" if from_example else url)


def _builder_context(request, document) -> dict[str, Any]:
    """Everything all three builder screens render."""
    draft = document.draft
    reconciliation = reconcile_service.reconcile(document)
    return {
        "document": document,
        "draft": draft,
        "doc": draft.content,
        # The preview is drawn from the same flattened document the exporters
        # consume, so "what you saw is what downloaded" is a property of the
        # code rather than a claim in the marketing copy.
        **presentation.paper(draft.content, document.template),
        "sections": content_rules.visible_sections(draft.content),
        "all_sections": draft.content.get("sections", []),
        "template": document.template,
        "templates": selectors.active_templates(),
        "versions": selectors.versions_with_summaries(document),
        # A list, so the improvement panels on the same page can reuse it.
        "suggestions": list(selectors.open_suggestions(document)),
        "exports": selectors.renders_for(document),
        "drift": reconciliation,
        # An unsaved draft says when it will be removed (docs/CV-Lifecycle.md).
        "document_expires_on": document_service.draft_expires_on(document),
        "status": {
            "dirty": draft.dirty,
            "saved_at": draft.saved_at,
            "row_version": draft.row_version,
            "version_no": document.current_version.version_no if document.current_version else 0,
            # The annotation is a snapshot of the moment the document was read;
            # a command changes the ring mid-request, so only a read trusts it.
            "can_undo": (
                document.has_snapshots
                if request.method == "GET" and hasattr(document, "has_snapshots")
                else draft.snapshots.exists()
            ),
        },
    }


class EditorView(CandidateResumeView):
    def get(self, request: HttpRequest, document_id: str | None = None) -> HttpResponse:
        document = self.document(request, document_id)
        if document is None:
            return redirect("resume:builder")

        document_service.set_mode(document=document, actor=request.user, mode=BuilderMode.EDITOR)
        return render(
            request,
            "resume/editor.html",
            {"page_title": document.name, **_builder_context(request, document)},
        )


def _improvement_context(
    document, *, mode: str, analysis: Any = improvement._UNSET, offered: list | None = None
) -> dict[str, Any]:
    """What the improvement panels on the guided and assistant screens read.

    `mode` is "guided" (work through the findings one at a time) or "assisted"
    (see what will change, then have it drafted). The plan and the job are
    read fresh on every render — they are rows, not page state, which is what
    lets a refresh or a return visit show exactly where things were left.
    """
    job = improvement_service.current_for(document)
    return {
        "document": document,
        "plan": improvement.plan_for(document, analysis=analysis, offered=offered),
        "mode": mode,
        "job": job,
        # A finished job stays on screen for a while, so its result is read;
        # a failed one until it is retried or replaced, because it needs a
        # decision.
        "job_visible": improvement_service.is_worth_showing(job),
        "job_stages": improvement_service.stages(job) if job is not None else [],
        # One per rendered form: a resubmitted form returns the job it already
        # made instead of queueing the same work twice.
        "form_token": uuid.uuid4().hex,
    }


class ImprovementPanelView(CandidateResumeView):
    """`/cv/builder/<id>/improve/` — the plan panel, redrawn when the CV changes."""

    def get(self, request: HttpRequest, document_id: str) -> HttpResponse:
        document = self.document(request, document_id)
        if document is None:
            raise Http404
        mode = "assisted" if request.GET.get("mode") == "assisted" else "guided"
        return render(
            request, "resume/_improvement_plan.html", _improvement_context(document, mode=mode)
        )


class ImprovementJobView(CandidateResumeView):
    """`/cv/builder/<id>/job/` — where the background work has got to.

    Polled by the job panel while work is running. When the job it was
    watching has finished, the answer carries an `improve:done` event, and the
    editor, the transcript and the plan redraw themselves from their own rows.
    """

    def get(self, request: HttpRequest, document_id: str) -> HttpResponse:
        document = self.document(request, document_id)
        if document is None:
            raise Http404
        context = _improvement_context(document, mode=request.GET.get("mode", "guided"))
        response = render(request, "resume/_improvement_job.html", context)
        job = context["job"]
        watching = request.GET.get("watching", "")
        if job is not None and not job.is_active and watching == str(job.public_id):
            response["HX-Trigger"] = json.dumps({"improve:done": str(job.public_id)})
        return response


class TranscriptView(CandidateResumeView):
    """`/cv/builder/<id>/transcript/` — the conversation, redrawn after a turn."""

    def get(self, request: HttpRequest, document_id: str) -> HttpResponse:
        document = self.document(request, document_id)
        if document is None:
            raise Http404
        session = builder_service.session_for(document=document, mode=BuilderMode.ASSISTANT)
        return render(
            request,
            "resume/_transcript.html",
            {
                "document": document,
                "transcript": session.transcript,
                **_improvement_context(document, mode="assisted"),
            },
        )


class EditorLiveView(CandidateResumeView):
    """`/cv/builder/<id>/live/` — the editor region, redrawn when drafts arrive."""

    def get(self, request: HttpRequest, document_id: str) -> HttpResponse:
        document = self.document(request, document_id)
        if document is None:
            raise Http404
        return render(request, "resume/_editor_live.html", _builder_context(request, document))


class GuidedBuilderView(CandidateResumeView):
    def get(self, request: HttpRequest, document_id: str | None = None) -> HttpResponse:
        document = self.document(request, document_id)
        if document is None:
            return redirect("resume:builder")

        document_service.set_mode(document=document, actor=request.user, mode=BuilderMode.GUIDED)
        guided = builder_service.guided_state(document=document)
        # Read once for the readiness line and the "What we found" panel.
        analysis = selectors.latest_analysis_for(document)
        builder = _builder_context(request, document)
        return render(
            request,
            "resume/guided.html",
            {
                "page_title": "Guided builder",
                "guided": guided,
                # The one "is it ready" answer (CVR-01). The counter above it
                # says how many steps have something in them; this says
                # whether the CV is actually complete, and the two used to be
                # read as the same thing. Here and on the analysis report only
                # — the builder screens sit under NFR-2's query ceiling.
                "readiness": readiness.assess(document, guided=guided, analysis=analysis),
                **builder,
                **_improvement_context(
                    document, mode="guided", analysis=analysis, offered=builder["suggestions"]
                ),
            },
        )


class AssistantView(CandidateResumeView):
    def get(self, request: HttpRequest, document_id: str | None = None) -> HttpResponse:
        if document_id is None and (
            request.GET.get("new")
            or not selectors.active_documents_for(self.profile(request)).exists()
        ):
            # The way in: one prominent prompt (docs/Assisted-CV.md).
            return render(request, "resume/assistant_start.html", _assistant_start_context(request))
        document = self.document(request, document_id)
        if document is None:
            return redirect("resume:builder")

        document_service.set_mode(document=document, actor=request.user, mode=BuilderMode.ASSISTANT)
        session = builder_service.session_for(document=document, mode=BuilderMode.ASSISTANT)
        builder = _builder_context(request, document)
        return render(
            request,
            "resume/assistant.html",
            {
                "page_title": "Assisted CV",
                "transcript": session.transcript,
                **builder,
                **_improvement_context(document, mode="assisted", offered=builder["suggestions"]),
            },
        )


#: Prompts a candidate can start from, one click to fill the box. Written as
#: a person would say it: what they want, and what they have done.
ASSISTANT_EXAMPLES = (
    "I want a CV for junior accountant roles. I have a BBS from Tribhuvan University "
    "and spent a year doing bookkeeping for a small trading company.",
    "Help me write a CV for a staff nurse job. I have worked three years on a medical "
    "ward at a district hospital.",
    "I am a Django developer with two years at a small agency and want to move into "
    "backend roles at a product company.",
    "I have just finished my BIT degree and need my first CV. My final-year project was "
    "an attendance app.",
)


def _assistant_start_context(request: HttpRequest, **extra: Any) -> dict[str, Any]:
    # `?new=1` reaches here before anything else has made sure the profile
    # exists, as it does for an employer opening the CV tools for the first time.
    profile = profile_service.get_or_create_profile(user=request.user)
    parsed = (
        selectors.uploads_for(profile).filter(parse_status="parsed").order_by("-created_at").first()
    )
    return {
        "page_title": "Assisted CV",
        "examples": ASSISTANT_EXAMPLES,
        "has_saved_material": parsed is not None or bool(profile.headline),
        "parsed_upload": parsed,
        # One per form shown: a double-click or a resubmitted form finds the
        # CV the first press started instead of starting a second one.
        "form_token": secrets.token_urlsafe(16),
        **extra,
    }


class AssistantStartView(CandidateResumeView):
    """`POST /cv/builder/describe/start/`: start a CV from one description.

    Creates an unsaved draft (docs/CV-Lifecycle.md) in assistant mode and
    sends the description as the first turn, answered in the background like
    every other turn. What the candidate already has (their parsed CV) is used
    only when they tick the box that says so.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        text = (request.POST.get("text") or "").strip()
        target = (request.POST.get("target") or "").strip()[:120]
        if len(text) < 15:
            return render(
                request,
                "resume/assistant_start.html",
                _assistant_start_context(
                    request,
                    error="Tell us a little more: the kind of job you want and something you have done.",
                    text=text,
                    target=target,
                ),
                status=400,
            )

        token = (request.POST.get("token") or "")[:64]
        if token:
            already = (
                ImprovementJob.objects.filter(document__profile=profile, request_token=token)
                .select_related("document")
                .first()
            )
            if already is not None:
                return redirect("resume:assistant", document_id=str(already.document.public_id))

        reuse = request.POST.get("reuse") == "1"
        content = None
        parsed = None
        if reuse:
            parsed = (
                selectors.uploads_for(profile)
                .filter(parse_status="parsed")
                .select_related("parse")
                .order_by("-created_at")
                .first()
            )
        if parsed is not None and getattr(parsed, "parse", None) is not None:
            content = document_service._content_from_parse(parsed.parse, profile=profile)
        else:
            content = content_rules.blank_document(
                name=profile.user.full_name,
                email=profile.user.email,
                headline=(profile.headline if reuse else "") or target,
            )

        try:
            document = document_service.create_document(
                profile=profile,
                actor=request.user,
                name=(f"CV for {target}" if target else "My CV")[:120],
                mode=BuilderMode.ASSISTANT,
                content=content,
            )
            if target:
                assistant_service.set_target(document=document, title=target, missing=[])
            improvement_service.submit(
                document=document,
                actor=request.user,
                kind=ImprovementJobKind.TURN,
                payload={"text": text},
                token=token,
            )
        except DomainError as exc:
            return render(
                request,
                "resume/assistant_start.html",
                _assistant_start_context(request, error=exc.message, text=text, target=target),
                status=400,
            )
        return redirect("resume:assistant", document_id=str(document.public_id))


class AssistantTargetView(CandidateResumeView):
    """`/cv/builder/<id>/target/` — work on a target job's gaps (ENH-01).

    The analysis report lists what a posting asks for that the CV does not
    mention. This hands that list to the assistant, which then asks about each
    item in turn — where the candidate used it and what they did — and drafts
    bullets only from what they answer. Nothing from the posting is added to
    the CV by this step: a gap is a question, not a fact.
    """

    def post(self, request: HttpRequest, document_id: str) -> HttpResponse:
        profile = self.profile(request)
        document = selectors.document_for(profile, document_id)
        if document is None:
            raise Http404
        analysis = selectors.analysis_for(profile, request.POST.get("analysis", ""))
        if analysis is None or analysis.version.document_id != document.pk:
            raise Http404

        target = analysis.target or {}
        title = (
            (analysis.job.title if analysis.job_id else "")
            or analysis.target_role
            or ("the role you are aiming at")
        )
        assistant_service.set_target(
            document=document, title=title, missing=_gaps_required_first(target)
        )
        url = reverse("resume:assistant", kwargs={"document_id": str(document.public_id)})
        return redirect(f"{url}?aimed=1")


class PrintPreviewView(CandidateResumeView):
    """The document alone, for printing. Rendering to PDF is P6."""

    def get(self, request: HttpRequest, document_id: str | None = None) -> HttpResponse:
        document = self.document(request, document_id)
        if document is None:
            return redirect("resume:builder")

        draft = document.draft
        return render(
            request,
            "resume/print.html",
            {
                "document": document,
                "doc": draft.content,
                **presentation.paper(draft.content, document.template),
                "sections": content_rules.visible_sections(draft.content),
                "template": document.template,
            },
        )


class BuilderCommandView(CandidateResumeView):
    """Every edit the builder screens can make.

    One endpoint with a `command` field: each change re-renders the same live
    region, because almost every edit also moves the preview and the save
    status, and swapping them separately lets them disagree.
    """

    def post(self, request: HttpRequest, document_id: str) -> HttpResponse:
        profile = self.profile(request)
        document = selectors.document_for(profile, document_id)
        if document is None:
            raise Http404

        policies.require_editable(document)
        handler = getattr(self, f"do_{request.POST.get('command', '')}", None)
        if handler is None:
            return HttpResponse("Unknown command", status=400)

        message = error = None
        try:
            message = handler(request, document)
        except DomainError as exc:
            error = exc

        document.refresh_from_db()
        response = _fragment(
            request,
            "resume/_editor_live.html",
            _builder_context(request, document),
            message=message,
            error=error,
        )
        if fragments.is_page_navigation(request):
            # A full-page post got a page (`common.fragments`); nothing to add.
            return response

        # Every change can settle a finding, and some queue background work:
        # the panels outside this region listen for these and redraw
        # themselves from their own rows (`ImprovementPanelView`,
        # `ImprovementJobView`, `TranscriptView`).
        events = json.loads(response.get("HX-Trigger") or "{}")
        events["cv:changed"] = True
        if getattr(request, "_improvement_queued", False):
            events["improve:queued"] = True
        response["HX-Trigger"] = json.dumps(events)

        if request.POST.get("command") in {"step", "step_aside"}:
            # The step list lives in the left nav, outside the region this
            # swaps. Without this the counter moved on the server and the old
            # number stayed on screen, which reads as the button not working.
            response.write(
                render_to_string(
                    "resume/_guided_steps.html",
                    {
                        "document": document,
                        "guided": builder_service.guided_state(document=document),
                        "oob": True,
                    },
                    request=request,
                )
            )

        return response

    # Each handler returns the confirmation sentence, or raises DomainError.

    def do_autosave(self, request, document) -> str:
        payload = json.loads(request.POST.get("content") or "{}")
        draft_service.autosave(
            draft=document.draft, content=payload, expected_version=_version(request)
        )
        return ""

    def do_contact(self, request, document) -> str:
        draft_service.set_contact(
            draft=document.draft,
            field=request.POST.get("field", ""),
            value=request.POST.get("value", ""),
        )
        return "Saved"

    def do_section_text(self, request, document) -> str:
        draft_service.set_section_text(
            draft=document.draft,
            section_id=request.POST.get("section", ""),
            text=request.POST.get("text", ""),
        )
        return "Saved"

    def do_section_items(self, request, document) -> str:
        draft_service.set_section_items(
            draft=document.draft,
            section_id=request.POST.get("section", ""),
            items=request.POST.getlist("items") or _lines(request.POST.get("text", "")),
        )
        return "Saved"

    def do_add_section(self, request, document) -> str:
        draft_service.add_section(
            draft=document.draft,
            title=request.POST.get("title", ""),
            kind=request.POST.get("kind", "entries"),
        )
        return "Section added"

    def do_rename_section(self, request, document) -> str:
        draft_service.rename_section(
            draft=document.draft,
            section_id=request.POST.get("section", ""),
            title=request.POST.get("title", ""),
        )
        return "Renamed"

    def do_toggle_section(self, request, document) -> str:
        visible = draft_service.toggle_section(
            draft=document.draft, section_id=request.POST.get("section", "")
        )
        return "Section shown" if visible else "Section hidden"

    def do_move_section(self, request, document) -> str:
        draft_service.move_section(
            draft=document.draft,
            section_id=request.POST.get("section", ""),
            offset=int(request.POST.get("offset", "0") or 0),
        )
        return "Moved"

    def do_reorder_sections(self, request, document) -> str:
        draft_service.reorder_sections(draft=document.draft, order=request.POST.getlist("order"))
        return "Order saved"

    def do_delete_section(self, request, document) -> str:
        draft_service.delete_section(
            draft=document.draft, section_id=request.POST.get("section", "")
        )
        return "Section removed"

    def do_add_entry(self, request, document) -> str:
        draft_service.add_entry(draft=document.draft, section_id=request.POST.get("section", ""))
        return "Entry added"

    def do_update_entry(self, request, document) -> str:
        draft_service.update_entry(
            draft=document.draft,
            entry_id=request.POST.get("entry", ""),
            title=request.POST.get("title", ""),
            org=request.POST.get("org", ""),
            dates=request.POST.get("dates", ""),
        )
        return "Saved"

    def do_move_entry(self, request, document) -> str:
        draft_service.move_entry(
            draft=document.draft,
            entry_id=request.POST.get("entry", ""),
            offset=int(request.POST.get("offset", "0") or 0),
        )
        return "Moved"

    def do_reorder_entries(self, request, document) -> str:
        draft_service.reorder_entries(
            draft=document.draft,
            section_id=request.POST.get("section", ""),
            order=request.POST.getlist("order"),
        )
        return "Order saved"

    def do_duplicate_entry(self, request, document) -> str:
        draft_service.duplicate_entry(draft=document.draft, entry_id=request.POST.get("entry", ""))
        return "Copied"

    def do_delete_entry(self, request, document) -> str:
        draft_service.delete_entry(draft=document.draft, entry_id=request.POST.get("entry", ""))
        return "Entry removed"

    def do_bullets(self, request, document) -> str:
        draft_service.set_bullets(
            draft=document.draft,
            entry_id=request.POST.get("entry", ""),
            bullets=request.POST.getlist("bullets") or _lines(request.POST.get("text", "")),
        )
        return "Saved"

    def do_undo(self, request, document) -> str:
        return "Undone" if draft_service.undo(draft=document.draft) else "Nothing to undo"

    def do_template(self, request, document) -> str:
        template = selectors.template_by_slug(request.POST.get("template", ""))
        if template is None:
            raise Http404
        document_service.set_template(document=document, actor=request.user, template=template)
        return f"Template: {template.name}"

    def do_save_version(self, request, document) -> str:
        version = version_service.finalize(
            document=document,
            actor=request.user,
            reason=VersionReason.SAVED,
            change_note=request.POST.get("note", ""),
        )
        return f"Saved version {version.version_no}"

    def do_restore_version(self, request, document) -> str:
        version = selectors.version_for(document, int(request.POST.get("version", 0) or 0))
        if version is None:
            raise Http404
        version_service.restore(document=document, actor=request.user, version=version)
        return f"Restored version {version.version_no}"

    def do_accept_suggestion(self, request, document) -> str:
        suggestion = document.draft.suggestions.filter(
            pk=int(request.POST.get("id", 0) or 0)
        ).first()
        if suggestion is None:
            raise Http404
        suggestion_service.accept(suggestion=suggestion, actor=request.user)
        return "Applied"

    def do_reject_suggestion(self, request, document) -> str:
        suggestion = document.draft.suggestions.filter(
            pk=int(request.POST.get("id", 0) or 0)
        ).first()
        if suggestion is None:
            raise Http404
        suggestion_service.reject(suggestion=suggestion, actor=request.user)
        return "Dismissed"

    def do_reconcile(self, request, document) -> str:
        offered = reconcile_service.offer_suggestions(document=document, actor=request.user)
        return f"{offered} suggestion(s) from your profile" if offered else "Nothing to reconcile"

    def do_rename(self, request, document) -> str:
        document_service.rename(
            document=document, actor=request.user, name=request.POST.get("name", "")
        )
        return "Renamed"

    def do_make_primary(self, request, document) -> str:
        document_service.make_primary(document=document, actor=request.user)
        return "This is now your main CV"

    def do_step(self, request, document) -> str:
        builder_service.set_step(document=document, step=request.POST.get("step", ""))
        return ""

    def do_step_aside(self, request, document) -> str:
        """Mark a guided step as not applying to this person, or take it back.

        Nothing is written to the CV. Somebody who has never had a job is
        recording that fact about themselves, and writing an empty experience
        section on their behalf would be the platform putting a claim on their
        document.
        """
        step = request.POST.get("step", "")
        state = builder_service.toggle_step_aside(document=document, step=step)
        aside = next((row for row in state["steps"] if row["key"] == step), None)
        if aside is None:
            return ""
        return "Set aside. You can bring it back any time" if aside["skipped"] else "Back on"

    def do_turn(self, request, document) -> str:
        """One turn of the assistant conversation, answered in the background.

        The answer is written to the transcript here, before anything else, so
        the screen can say it was received; the reply and any drafted bullets
        come from the `ai` queue (`services.improvements`), and the job panel
        shows each stage. A second submission while one is running returns
        that one rather than queueing another.
        """
        job, created = improvement_service.submit(
            document=document,
            actor=request.user,
            kind=ImprovementJobKind.TURN,
            payload={
                "text": request.POST.get("text", ""),
                "focus_entry": improvement_service.take_focus(document),
            },
            token=request.POST.get("token", ""),
        )
        request._improvement_queued = True
        if not created:
            return "Still working on your last answer, shown below"
        return "Answer received. Working on it"

    def _plan_item(self, request, document):
        plan = improvement.plan_for(document)
        item = improvement.item_by_key(plan, request.POST.get("key", ""))
        if item is None:
            raise InvalidState("That point is no longer on the list; the CV has changed since.")
        return plan, item

    def do_improve_answer(self, request, document) -> str:
        """An answer to one finding's question, drafted into the entry it is about."""
        _plan, item = self._plan_item(request, document)
        if not item.can_answer:
            raise InvalidState("That one is changed in the editor, not answered.")
        _job, created = improvement_service.submit(
            document=document,
            actor=request.user,
            kind=ImprovementJobKind.TURN,
            payload={
                "text": request.POST.get("text", ""),
                "focus_entry": item.entry_id,
                "question": f"{item.title}. {item.first_question}",
                "finding": item.key,
            },
            token=request.POST.get("token", ""),
        )
        request._improvement_queued = True
        return (
            "Answer received. Drafting from what you said" if created else "Already working on it"
        )

    def do_improve_contact(self, request, document) -> str:
        """Fill a missing contact field. Only an empty one: this never overwrites."""
        _plan, item = self._plan_item(request, document)
        if not item.can_fill_contact:
            raise InvalidState("That detail is already on your CV.")
        value = request.POST.get("value", "").strip()
        if not value:
            raise InvalidState("Type it in first.")
        draft_service.set_contact(draft=document.draft, field=item.contact_field, value=value)
        return f"Added your {item.contact_field}"

    def do_improve_ask(self, request, document) -> str:
        """Put one finding's question into the conversation."""
        _plan, item = self._plan_item(request, document)
        improvement_service.ask(
            document=document,
            question=f"{item.title}. {item.first_question}",
            focus_entry=item.entry_id if item.can_answer else "",
        )
        request._improvement_queued = True
        return "Asked below. Answer in your own words"

    def do_improve_prepare(self, request, document) -> str:
        """Draft the plan's safe rewordings, in the background."""
        keys = [key for key in request.POST.getlist("keys") if key]
        _job, created = improvement_service.submit(
            document=document,
            actor=request.user,
            kind=ImprovementJobKind.PREPARE,
            payload={"keys": keys},
            token=request.POST.get("token", ""),
        )
        request._improvement_queued = True
        return "Preparing your improvements" if created else "Already working on it"

    def do_improve_apply(self, request, document) -> str:
        return self._decide_all(request, document, accept=True)

    def do_improve_reject(self, request, document) -> str:
        return self._decide_all(request, document, accept=False)

    def _decide_all(self, request, document, *, accept: bool) -> str:
        ids = [int(value) for value in request.POST.getlist("ids") if value.isdigit()]
        if not ids:
            raise InvalidState("There is nothing waiting to apply.")
        outcome = improvement_service.decide_all(
            document=document, actor=request.user, ids=ids, accept=accept
        )
        verb = "Applied" if accept else "Kept your wording on"
        text = f"{verb} {outcome['done']} change{'s' if outcome['done'] != 1 else ''}"
        if outcome["refused"]:
            text += (
                f"; {outcome['refused']} could not be applied because that part of your CV"
                " changed after it was drafted"
            )
        return text

    def do_job_retry(self, request, document) -> str:
        job = document.improvement_jobs.filter(public_id=request.POST.get("job", "")).first()
        if job is None:
            raise Http404
        improvement_service.retry(job=job, actor=request.user)
        request._improvement_queued = True
        return "Trying again"


class DocumentCommandView(CandidateResumeView):
    """Document-level actions from the hub: archive, restore, delete, duplicate."""

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        document = selectors.document_for(profile, request.POST.get("document", ""))
        if document is None:
            raise Http404

        command = request.POST.get("command", "")
        actions = {
            "archive": document_service.archive,
            "restore": document_service.restore,
            "delete": document_service.delete,
            "duplicate": document_service.duplicate,
            "make_primary": document_service.make_primary,
        }
        action = actions.get(command)
        if action is None:
            return HttpResponse("Unknown command", status=400)

        message = error = None
        try:
            action(document=document, actor=request.user)
            message = DOCUMENT_COMMAND_DONE[command]
        except DomainError as exc:
            error = exc

        return _fragment(
            request,
            "resume/_document_list.html",
            _document_list_context(profile),
            message=message,
            error=error,
        )


#: What each document command says when it has worked.
DOCUMENT_COMMAND_DONE = {
    "archive": "Archived",
    "restore": "Restored",
    "delete": "Deleted",
    "duplicate": "Duplicated as a new draft",
    "make_primary": "Set as your main CV",
}


def _document_list_context(profile) -> dict[str, Any]:
    """Saved CVs, unsaved drafts and archived CVs (docs/CV-Lifecycle.md)."""
    return {
        "documents": selectors.saved_documents_for(profile),
        "drafts": selectors.drafts_for(profile),
        "archived": selectors.documents_for(profile).filter(archived_at__isnull=False),
    }


class AnalysisView(CandidateResumeView):
    """`/cv/analysis/` — the report, the checks, and the score over time.

    Reads the latest analysis for the chosen document. It does not run one: an
    analysis freezes a version and may call a model, and neither belongs on a
    GET that a refresh repeats.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        chosen = request.GET.get("document") or None
        if not chosen and not request.GET.get("report"):
            # "Latest analysis" opens the latest analysis, whichever CV it is
            # of — not the main CV's (REF-01).
            latest = selectors.latest_analysis_of_profile(profile)
            if latest is not None:
                chosen = str(latest.version.document.public_id)
        document = self.document(request, chosen)

        analysis = None
        history: list[dict[str, Any]] = []

        # The named-report lookup is outside the `document is not None` branch
        # deliberately. Inside it, somebody with no CV of their own asking for
        # another candidate's report by id fell through to a bare 200 — the
        # scoped lookup was never reached, so nothing refused them.
        named = request.GET.get("report")
        if named:
            analysis = selectors.analysis_for(profile, named)
            if analysis is None:
                raise Http404
            document = analysis.version.document
        elif document is not None:
            analysis = selectors.latest_analysis_for(document)

        if document is not None:
            history = analysis_service.history_for(document)

        return render(
            request,
            "resume/analysis.html",
            {
                "page_title": "CV analysis",
                "documents": selectors.active_documents_for(profile).prefetch_related("versions"),
                **_report_context(document, analysis),
                "history": history,
                # How many runs are left, so the screen can say so before the
                # click rather than after it. A free plan grants one analysis a
                # month, and the button used to look identical whether it was
                # about to produce a report or a refusal.
                "analyses_left": entitlements.remaining(profile, limits.RESUME_ANALYSES.key),
                "target_jobs": _target_job_options(profile),
                **_cv_jobs(profile, document),
                # The hidden review watcher's own request (POLL-01): when the
                # review is no longer coming, its answer reloads the report once.
                "watching": request.GET.get("watch") == "1",
            },
        )


def _cv_jobs(profile, document) -> dict[str, Any]:
    """Jobs this candidate fits, beside the report of their CV (MAT-02).

    From the matching feed — the same scores "Matched to me" shows, not a
    second ranking. Matching reads the *profile* (ADR-A46), not a CV, so the
    report says that, and names any skill the CV lists that the profile does
    not: those are the ones matching cannot see until the candidate adds them.
    """
    from apps.core.features import is_enabled
    from apps.matching.services import recommendations

    if document is None or not is_enabled("job_matching"):
        return {"cv_jobs": [], "unlisted_skills": []}

    from apps.jobs.models import Job
    from apps.resumes.analysis import target as target_rules
    from apps.resumes.analysis import text as text_rules

    feed = recommendations.feed_for(profile)
    items = feed.items[:3]
    # Why each job fits, and what it would still want, from *this CV* (MAT-03):
    # the same comparison readiness makes, so "✓ Python" here and "requires
    # Django" there never disagree. The score beside it is still matching's.
    jobs = Job.objects.filter(pk__in=[item.job.pk for item in items]).prefetch_related(
        "skills__skill"
    )
    by_pk = {job.pk: job for job in jobs}
    cv_text = text_rules.all_text(document.draft.content).lower()
    cv_jobs = []
    for item in items:
        covered = target_rules.coverage(target_rules.from_job(by_pk[item.job.pk]), cv_text)
        required = covered.get("missing_required", [])
        cv_jobs.append(
            {
                "title": item.job.title,
                "company": item.job.company.name,
                "slug": item.job.slug,
                "label": item.match.band and matching_label(item.match.band),
                "score": item.match.score,
                "matched": covered["matched"][:6],
                "missing_required": required[:6],
                "missing_preferred": [n for n in covered["missing"] if n not in required][:4],
            }
        )

    on_profile = {name.lower() for name in profile.skills.values_list("skill__name", flat=True)}
    cv_skills = content_rules.section_by_key(document.draft.content, "skills") or {}
    unlisted = [
        item
        for item in (cv_skills.get("items") or [])
        if isinstance(item, str) and item.strip() and item.strip().lower() not in on_profile
    ]
    return {"cv_jobs": cv_jobs, "unlisted_skills": unlisted[:8]}


def matching_label(band: str) -> str:
    from apps.matching import scoring

    return scoring.label_for(band)


def _target_job(public_id: str):
    """A published posting to analyse against, or None."""
    from apps.common.ids import as_uuid
    from apps.jobs.models import Job, JobStatus

    public_id = as_uuid(public_id)
    if public_id is None:
        return None
    return Job.objects.filter(
        public_id=public_id, status=JobStatus.PUBLISHED, deleted_at__isnull=True
    ).first()


def _target_job_options(profile) -> list:
    """Postings worth offering as a target: applied to, saved, then newest."""
    from apps.jobs.models import Job, JobStatus

    live = Job.objects.filter(status=JobStatus.PUBLISHED, deleted_at__isnull=True).select_related(
        "company"
    )
    chosen: list = []
    seen: set[int] = set()
    for queryset in (
        live.filter(applications__candidate=profile),
        live.filter(saved_by__profile=profile),
        live.order_by("-published_at"),
    ):
        for job in queryset.distinct()[:15]:
            if job.pk not in seen and len(chosen) < 15:
                seen.add(job.pk)
                chosen.append(job)
    return chosen


def _queue_enrichment(analysis, actor) -> None:
    """Ask for the model's half, off the request.

    Swallowed rather than raised: a broker that is down must not turn a
    complete deterministic report into an error page. The report says it is
    still being read, and stops saying so when the task lands or fails.
    """
    from apps.resumes.tasks import enrich_analysis

    try:
        enrich_analysis.delay(analysis.pk, actor.pk if actor else None)
    except Exception:  # noqa: BLE001 - a queue outage is not the candidate's problem
        logger.warning("could not queue enrichment for analysis %s", analysis.pk, exc_info=True)


def _report_context(document, analysis) -> dict[str, Any]:
    """Everything the report renders.

    One helper rather than three literals: the report is returned by the page,
    by the run and by every accept, and a key added to one of those three is a
    section that silently vanishes from the other two.
    """
    return {
        "document": document,
        "analysis": analysis,
        # Poll for the written review only while it could plausibly still be
        # coming (CVP-02). The report used to re-render every three seconds
        # for as long as the review was pending, which with no worker running
        # is for ever: every button on it was replaced under the pointer, and
        # keyboard focus went back to the top of the page every three seconds.
        "poll_review": _review_may_still_arrive(analysis),
        "findings": _grouped_findings(analysis),
        "checks": analysis.checks.all() if analysis else [],
        "history": analysis_service.history_for(document) if document else [],
        # Pending suggestions belong on this screen, not only in the editor.
        # "Draft a fix" used to end with "accept it in the editor", which sent
        # the candidate to a different page to finish the thing they were in
        # the middle of, and most of them did not go.
        "suggestions": selectors.open_suggestions(document) if document else [],
        "readiness": readiness.assess(document, analysis=analysis) if document else None,
        "improve": _improve_summary(analysis),
    }


def _improve_summary(analysis) -> dict[str, int]:
    """How many of this report's findings are rewordings, and how many need facts."""
    rows = [
        row for row in (analysis.findings.all() if analysis else []) if row.severity != "strength"
    ]
    reword = sum(
        1
        for row in rows
        if improvement.GUIDANCE.get(
            row.code, improvement.Guidance(improvement.NEEDS_INPUT, "")
        ).kind
        == improvement.REWORD
    )
    return {"total": len(rows), "reword": reword, "input": len(rows) - reword}


#: How long the report keeps asking whether the written review has arrived.
#: A healthy worker writes it in well under this; past it, the candidate is
#: told to come back rather than watched by a spinner.
REVIEW_POLL_MINUTES = 6  # the review task's 300 s timeout, plus queueing time


def _review_may_still_arrive(analysis) -> bool:
    import datetime as dt

    from django.utils import timezone

    if analysis is None or analysis.enrichment != "pending":
        return False
    return timezone.now() - analysis.created_at < dt.timedelta(minutes=REVIEW_POLL_MINUTES)


class AnalysisRunView(CandidateResumeView):
    """`/cv/analysis/run/` — freeze a version and produce a report.

    Synchronous, like export and for the same reason: the candidate pressed a
    button and is watching. The deterministic half is milliseconds; the model
    call is the slow part, and the runner's own timeout bounds it. The Celery
    path (`resumes.tasks.analyse_version`) exists for anything not on a request.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        document = self.document(request, request.POST.get("document") or None)
        if document is None:
            raise Http404

        target_role = (request.POST.get("target_role") or "").strip()
        advert = (request.POST.get("advert") or "").strip()
        # A posting on CareerAI, when one was picked (ENH-01). The service has
        # always accepted a job and compared against its structured skills;
        # no screen ever passed one, so the only way to target a role was to
        # paste its advert — including for jobs that are on this platform.
        target_job = _target_job(request.POST.get("job") or "")
        message = error = None
        try:
            policies.require_editable(document)
            # Deterministic now, prose later. The score, the applicant-tracking
            # checks, the measured findings and the advert coverage are all
            # ready in milliseconds; the model's verdict is ninety seconds on a
            # modest provider, and a candidate does not sit through ninety
            # seconds of spinner — they close the tab and conclude it is broken.
            analysis = analysis_service.analyse(
                document=document,
                actor=request.user,
                target_role=target_role,
                advert=advert,
                job=target_job,
                defer_ai=True,
            )
            _queue_enrichment(analysis, request.user)
            # Re-read before rendering. A worker that is fast — or eager, as in
            # tests — has already written the prose by now, and rendering the
            # stale in-memory row would tell the candidate their review is
            # still coming when it is sitting in the database.
            analysis.refresh_from_db()
            covered = analysis.target or {}
            if covered.get("percent") is not None:
                message = (
                    f"Scored {analysis.score} out of 100 · "
                    f"{covered['percent']}% of what that advert asks for"
                )
            else:
                message = f"Scored {analysis.score} out of 100"
        except DomainError as exc:
            error = exc
            analysis = selectors.latest_analysis_for(document)

        response = _fragment(
            request,
            "resume/_analysis_report.html",
            _report_context(document, analysis),
            message=message,
            error=error,
        )
        if error is None and analysis is not None:
            # The address bar names the report that is now on screen, so a
            # refresh, a bookmark or the back button returns to it (REF-01).
            response["HX-Replace-Url"] = f"{reverse('resume:analysis')}?report={analysis.public_id}"
        return response


class FindingFixView(CandidateResumeView):
    """`/cv/analysis/fix/` — draft a fix, then accept or reject it, here.

    Nothing on this path edits a CV directly. Drafting writes a
    `ResumeSuggestion`; accepting is the same accept the reconcile protocol
    uses, and is the reason nothing a model wrote can reach a document without
    a person saying yes (INV-3).

    Accept and reject used to live only in the editor, so the report ended
    every drafted fix with "accept it in the editor" — a different screen,
    reached by a link, in the middle of the one task the candidate came here to
    do. The write path is unchanged; only the place it can be triggered from is.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        command = request.POST.get("command") or "fix"

        if command == "fix":
            return self._fix(request, profile)
        if command in ("accept", "reject"):
            return self._decide(request, profile, command)
        if command == "retry_review":
            return self._retry_review(request, profile)
        return HttpResponse("Unknown command", status=400)

    def _retry_review(self, request: HttpRequest, profile) -> HttpResponse:
        """Ask for the written review again, for free.

        The only way to retry used to be "Analyse this CV", which claims a
        monthly allowance — so one timeout on the platform's side spent a free
        candidate's single analysis for the month, and what they lost it for
        was the half that had already worked.
        """
        analysis = selectors.analysis_for(profile, request.POST.get("analysis") or "")
        if analysis is None:
            raise Http404

        message = error = None
        try:
            analysis = analysis_service.retry_enrichment(analysis=analysis, actor=request.user)
            message = (
                "The written review is here"
                if analysis.has_ai
                else "Still no luck. The score and every check below are unaffected"
            )
        except DomainError as exc:
            error = exc

        return self._report(
            request, analysis.version.document, analysis, message=message, error=error
        )

    def _fix(self, request: HttpRequest, profile) -> HttpResponse:
        finding = selectors.finding_for(profile, int(request.POST.get("finding") or 0))
        if finding is None:
            raise Http404

        analysis = finding.analysis
        document = analysis.version.document
        message = error = None
        try:
            policies.require_editable(document)
            analysis_service.apply_finding(finding=finding, actor=request.user)
            message = "Drafted below. Nothing changes until you accept it."
        except DomainError as exc:
            error = exc

        return self._report(request, document, analysis, message=message, error=error)

    def _decide(self, request: HttpRequest, profile, command: str) -> HttpResponse:
        document = selectors.document_for(profile, request.POST.get("document") or "")
        if document is None:
            raise Http404

        suggestion = document.draft.suggestions.filter(pk=int(request.POST.get("id") or 0)).first()
        if suggestion is None:
            raise Http404

        analysis = selectors.latest_analysis_for(document)
        message = error = None
        try:
            policies.require_editable(document)
            if command == "accept":
                suggestion_service.accept(suggestion=suggestion, actor=request.user)
                message = "Applied to your CV"
            else:
                suggestion_service.reject(suggestion=suggestion, actor=request.user)
                message = "Kept your wording"
        except DomainError as exc:
            error = exc

        return self._report(request, document, analysis, message=message, error=error)

    def _report(self, request, document, analysis, *, message, error) -> HttpResponse:
        return _fragment(
            request,
            "resume/_analysis_report.html",
            _report_context(document, analysis),
            message=message,
            error=error,
        )


#: Severity, and what the report calls it. Ordered, because a report that opens
#: with what is working reads as an excuse.
FINDING_GROUPS = (
    ("critical", "Fix these"),
    ("improve", "Worth improving"),
    ("strength", "Working well"),
)


def _grouped_findings(analysis) -> list[dict[str, Any]]:
    """Findings by severity, in the order the report reads them.

    Grouped in Python rather than by three queries or a template `regroup`: the
    findings are already prefetched, and the ordering is a product decision that
    belongs somewhere a person can read it.
    """
    rows = list(analysis.findings.all()) if analysis is not None else []
    return [
        {
            "key": key,
            "label": label,
            "rows": [finding for finding in rows if finding.severity == key],
        }
        for key, label in FINDING_GROUPS
    ]


# --- export and download --------------------------------------------------


class ExportView(CandidateResumeView):
    """`/cv/<id>/export/` — freeze the CV and produce a file.

    Synchronous on purpose: the candidate is watching, a CV renders in tens of
    milliseconds, and a spinner that resolves into a background job for work
    this small is worse than waiting for it. The Celery path exists for bulk
    and for retries (`resumes.tasks.render_version`), on the dedicated render
    queue so a burst cannot touch page latency (SRS §17.5).
    """

    def post(self, request: HttpRequest, document_id: str) -> HttpResponse:
        profile = self.profile(request)
        document = selectors.document_for(profile, document_id)
        if document is None:
            raise Http404

        policies.require_editable(document)
        fmt = request.POST.get("format", RenderFormat.PDF)

        message = error = None
        try:
            record = render_service.export(document=document, actor=request.user, fmt=fmt)
            message = f"{record.get_format_display()} ready"
        except DomainError as exc:
            error = exc

        document.refresh_from_db()
        context = _builder_context(request, document)
        context["exports"] = selectors.renders_for(document)
        return _fragment(request, "resume/_exports.html", context, message=message, error=error)


class DownloadView(CandidateResumeView):
    """`/cv/download/<id>/` — hand over a rendered file.

    The ownership check happens here, in the domain's own terms, and the bytes
    are served by `apps.files` behind a single-use token. Two gates rather than
    one: this view knows whose CV it is, and the file view knows whether the
    token is valid and logs the read (SRS §14.2).
    """

    def get(self, request: HttpRequest, render_id: int) -> HttpResponse:
        profile = self.profile(request)
        record = selectors.render_for(profile, render_id)
        if record is None:
            raise Http404

        policies.require_render_owner(record, profile)

        token = file_signing.issue(
            record.attachment, actor_id=request.user.pk, purpose="resume-export"
        )
        url = reverse("files:access", kwargs={"public_id": record.attachment.public_id})
        return redirect(f"{url}?t={token}")


class ExportHistoryView(CandidateResumeView):
    """Every file this candidate has exported, newest first."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        return render(
            request,
            "resume/exports.html",
            {
                "page_title": "Your exports",
                "exports": selectors.renders_for_profile(profile),
            },
        )


# --- templates ------------------------------------------------------------


def _locked_templates(profile) -> frozenset[str]:
    """The Pro templates this candidate's plan does not include.

    Read from the same entitlement the apply path enforces
    (`documents.require_template_allowed`), so the page and the service agree
    about what "locked" means. The page only decides what to offer; the
    service still refuses a direct post.
    """
    if entitlements.allows(profile, "pro_templates"):
        return frozenset()
    return frozenset(
        selectors.active_templates().filter(tier=TemplateTier.PRO).values_list("slug", flat=True)
    )


class TemplateGalleryView(CandidateResumeView):
    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        return render(
            request,
            "resume/templates.html",
            {
                "page_title": "Templates",
                **gallery.context(
                    request, selectors.active_templates(), locked=_locked_templates(profile)
                ),
            },
        )


class TemplateDetailView(CandidateResumeView):
    """One template at full size.

    Two different documents can be on this page, and the page says which:

      * **the worked example** — always, by default. It is sample data and
        stays the same whatever the candidate does to their own CV;
      * **the candidate's own CV** — only when they ask (`?cv=<document>`),
        drawn from that document's draft. Asking is the whole difference: a
        preview that silently swapped the sample for their draft made the
        template gallery change every time they edited their CV.
    """

    def get(self, request: HttpRequest, slug: str) -> HttpResponse:
        template = selectors.template_by_slug(slug)
        if template is None:
            raise Http404

        profile = self.profile(request)
        gallery.record_view(request, slug)
        documents = list(selectors.active_documents_for(profile))

        own = None
        wanted = request.GET.get("cv", "")
        if wanted:
            own = next((doc for doc in documents if str(doc.public_id) == wanted), None)
            if own is None:
                raise Http404

        # A template's own page shows somebody it was designed for (a nurse in
        # Plainspoke, a designer in Prism); the gallery keeps one shared example.
        content = (
            own.draft.content
            if own is not None
            else example_personas.for_template(template.slug) or presentation.sample_content()
        )
        return render(
            request,
            "resume/template_detail.html",
            {
                "page_title": template.name,
                "template": template,
                **presentation.paper(content, template),
                "documents": documents,
                "previewing": own,
                "is_own_cv": own is not None,
                "is_locked": slug in _locked_templates(profile),
                "is_favourite": gallery.is_favourite(request, slug),
                "compare": gallery.compare_set(request),
            },
        )


class TemplateCompareView(CandidateResumeView):
    def get(self, request: HttpRequest) -> HttpResponse:
        return render(
            request,
            "resume/compare.html",
            {"page_title": "Compare templates", **gallery.compare_context(request)},
        )


class TemplateCommandView(CandidateResumeView):
    def post(self, request: HttpRequest) -> HttpResponse:
        command = request.POST.get("command", "")
        slug = request.POST.get("slug", "")
        profile = self.profile(request)
        message = error = None
        next_url = ""

        if command == "favourite":
            on = gallery.toggle_favourite(request, slug)
            message = "Saved to favourites" if on else "Removed from favourites"
        elif command == "compare":
            _added, error = gallery.toggle_compare(request, slug)
            message = None if error else "Comparison updated"
        elif command == "clear_compare":
            gallery.clear_compare(request)
            message = "Comparison cleared"
        elif command == "apply":
            document = selectors.document_for(profile, request.POST.get("document", ""))
            template = selectors.template_by_slug(slug)
            if document is None or template is None:
                raise Http404
            try:
                document_service.set_template(
                    document=document, actor=request.user, template=template
                )
                message = f"{template.name} applied to {document.name}"
                # Applying a template is the way into editing in it: the
                # candidate lands in the editor, drawn in the new layout.
                next_url = reverse("resume:editor", kwargs={"document_id": str(document.public_id)})
            except DomainError as exc:
                error = exc
        else:
            return HttpResponse("Unknown command", status=400)

        return _fragment(
            request,
            "resume/_template_results.html",
            gallery.context(
                request, selectors.active_templates(), locked=_locked_templates(profile)
            ),
            message=message,
            error=error,
            fallback_url=reverse("resume:templates"),
            next_url=next_url,
        )


# --- guest upload ---------------------------------------------------------


class GuestUploadView(View):
    """`/cv/start/` — upload before registering (ADR-A32).

    The file is stored and scanned and nothing else. The token goes in the
    session so that registering can claim it; nothing reads the file until it
    is claimed.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("resume:upload")
        return render(request, "resume/guest_upload.html", {"page_title": "Start with your CV"})

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("resume:upload")

        uploaded = request.FILES.get("file")
        if uploaded is None:
            return render(
                request,
                "resume/guest_upload.html",
                {"page_title": "Start with your CV", "error": "Choose a file first."},
                status=400,
            )

        try:
            _upload, token = upload_service.upload_as_guest(uploaded_file=uploaded)
        except (UnsupportedFileType, FileTooLarge, DomainError) as exc:
            title, message = _upload_refusal(exc)
            return render(
                request,
                "resume/guest_upload.html",
                {"page_title": "Start with your CV", "error_title": title, "error": message},
                status=400,
            )

        tokens = request.session.get(GUEST_TOKENS_KEY, [])
        request.session[GUEST_TOKENS_KEY] = [*tokens, token][-5:]
        request.session.modified = True

        return render(
            request,
            "resume/guest_upload.html",
            {
                "page_title": "Start with your CV",
                "stored": True,
                "message": "Your CV is stored. Create an account to read it.",
            },
        )


def claim_guest_uploads(request: HttpRequest) -> int:
    """Claim everything this browser uploaded before registering.

    Called from the candidate's first authenticated page load. Returns how many
    arrived, which is the only number worth telling them.
    """
    tokens = request.session.pop(GUEST_TOKENS_KEY, [])
    if not tokens or not request.user.is_authenticated:
        return 0

    profile = profile_service.get_or_create_profile(user=request.user)
    claimed = claim_service.claim_all_for(tokens=tokens, profile=profile, actor=request.user)
    request.session.modified = True
    return len(claimed)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def _version(request) -> int | None:
    raw = request.POST.get("row_version")
    return int(raw) if raw and raw.isdigit() else None
