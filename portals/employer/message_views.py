"""
Employer messaging screens, URL-scoped by company (ADR-A18).

Opening a thread is gated on a live `CandidateVisibilityGrant` in the service,
not here, so the same rule holds for any other caller. What these views add is
the shape of the screen: a list on the left, one thread on the right, and no
way to start a conversation from a page that does not already have a candidate
on it — because a "message anyone" box is the interface bulk outreach wants.
"""

from __future__ import annotations

from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.applications import selectors as application_selectors
from apps.common import restrictions
from apps.common.errors import DomainError
from apps.companies import policies as company_policies
from apps.messaging import policies, presentation, selectors
from apps.messaging.models import ReportReason, SenderKind
from apps.messaging.services import conversations as conversation_service


def _error(request: HttpRequest, exc: DomainError, fallback: str) -> HttpResponse:
    restrictions.flash(request, exc)
    return redirect(fallback)


@method_decorator(login_required, name="dispatch")
class InboxView(View):
    """`/employer/<slug>/messages/` — every thread this company has."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_section(request.membership, "messages")
        threads = selectors.conversations_for_company(membership)

        chosen = None
        if request.GET.get("thread"):
            chosen = selectors.conversation_for_company(membership, request.GET["thread"])
            if chosen is None:
                raise Http404("No such conversation.")
            policies.require_company_side(chosen, membership)
            conversation_service.mark_read(conversation=chosen, side=SenderKind.COMPANY)
            conversation_service.share_thread_files_with(conversation=chosen, user=request.user)

        return render(
            request,
            "employer/messages/inbox.html",
            {
                "company": request.company,
                "membership": membership,
                "threads": threads,
                "thread": chosen,
                "rows": (
                    presentation.thread_rows(selectors.messages_in(chosen), viewer=request.user)
                    if chosen
                    else []
                ),
                "own_kind": SenderKind.COMPANY,
                "max_attachments": conversation_service.MAX_ATTACHMENTS,
                "edit_window": conversation_service.EDIT_WINDOW_MINUTES,
                "reasons": ReportReason.choices,
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_section(request.membership, "messages")
        thread = selectors.conversation_for_company(membership, request.POST.get("thread", ""))
        if thread is None:
            raise Http404("No such conversation.")

        back = f"{reverse('employer:messages', args=[company_slug])}?thread={thread.public_id}"
        action = request.POST.get("action") or "send"

        try:
            policies.require_company_side(thread, membership)
            if action == "send":
                conversation_service.send(
                    conversation=thread,
                    actor=request.user,
                    sender_kind=SenderKind.COMPANY,
                    body=request.POST.get("body", ""),
                    uploads=request.FILES.getlist("attachments"),
                )
            elif action in {"edit", "delete"}:
                django_messages.success(
                    request,
                    presentation.own_message_action(
                        thread=thread,
                        actor=request.user,
                        action=action,
                        message_id=request.POST.get("message_id", ""),
                        body=request.POST.get("body", ""),
                    ),
                )
            elif action == "mute":
                conversation_service.set_mute(
                    conversation=thread,
                    side=SenderKind.COMPANY,
                    muted=thread.company_muted_at is None,
                )
            elif action == "archive":
                conversation_service.archive(conversation=thread, actor=request.user)
                django_messages.success(request, "Archived.")
            elif action == "report":
                conversation_service.report(
                    conversation=thread,
                    actor=request.user,
                    reason=request.POST.get("reason", ""),
                    detail=request.POST.get("detail", ""),
                )
                django_messages.success(request, "Reported. Our team will look at it.")
            else:
                raise Http404("Unknown action.")
        except DomainError as exc:
            return _error(request, exc, back)

        return redirect(back)


@method_decorator(login_required, name="dispatch")
class StartConversationView(View):
    """`/employer/<slug>/applicants/<public_id>/message/`.

    Reachable only from an application, which is what makes the visibility
    grant a certainty rather than a check that usually passes.
    """

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_section(request.membership, "messages")
        application = application_selectors.application_for(membership, public_id)
        if application is None:
            raise Http404("No such application.")

        back = reverse("employer:application_detail", args=[company_slug, public_id])
        try:
            thread = conversation_service.open_conversation(
                company=request.company,
                candidate=application.candidate,
                actor=request.user,
                membership=membership,
                job=application.job,
                application=application,
                subject=request.POST.get("subject", ""),
            )
            body = (request.POST.get("body") or "").strip()
            if body:
                conversation_service.send(
                    conversation=thread,
                    actor=request.user,
                    sender_kind=SenderKind.COMPANY,
                    body=body,
                )
        except DomainError as exc:
            return _error(request, exc, back)

        return redirect(
            f"{reverse('employer:messages', args=[company_slug])}?thread={thread.public_id}"
        )
