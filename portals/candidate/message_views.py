"""
Candidate messaging screens (SRS §12).

The candidate side carries the controls that matter: mute, block, report. They
are on the thread rather than behind a settings page, because the moment
somebody needs them is the moment they are reading the message.

Blocking is deliberately not one click away from muting. Muting is reversible
and invisible; blocking ends the thread, hides the profile from that company
and revokes every grant they hold. Putting them side by side as equals would
get one of them pressed by mistake.
"""

from __future__ import annotations

from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.candidates.services import profile as profile_service
from apps.common import restrictions
from apps.common.errors import DomainError
from apps.messaging import policies, presentation, selectors
from apps.messaging.models import ReportReason, SenderKind
from apps.messaging.services import conversations as conversation_service


def _profile(request: HttpRequest):
    # Created on first use, as every other candidate screen does. The inbox is in
    # the rail for every account and answered 404 until the dashboard had been
    # opened once.
    return profile_service.get_or_create_profile(user=request.user)


@method_decorator(login_required, name="dispatch")
class InboxView(View):
    """`/messages/` — every company that has written to you."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _profile(request)
        threads = selectors.conversations_for_candidate(profile)

        chosen = None
        if request.GET.get("thread"):
            chosen = selectors.conversation_for_candidate(profile, request.GET["thread"])
            if chosen is None:
                raise Http404("No such conversation.")
            conversation_service.mark_read(conversation=chosen, side=SenderKind.CANDIDATE)

        return render(
            request,
            "messages/inbox.html",
            {
                "page_title": "Messages",
                "threads": threads,
                "thread": chosen,
                "rows": (
                    presentation.thread_rows(selectors.messages_in(chosen), viewer=request.user)
                    if chosen
                    else []
                ),
                "own_kind": SenderKind.CANDIDATE,
                "max_attachments": conversation_service.MAX_ATTACHMENTS,
                "edit_window": conversation_service.EDIT_WINDOW_MINUTES,
                "reasons": ReportReason.choices,
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _profile(request)
        thread = selectors.conversation_for_candidate(profile, request.POST.get("thread", ""))
        if thread is None:
            raise Http404("No such conversation.")

        back = f"{reverse('messages:inbox')}?thread={thread.public_id}"
        action = request.POST.get("action") or "send"

        try:
            # The selector already scoped by owner; the policy says the same
            # thing, and is what still refuses the day the selector widens.
            policies.require_candidate_side(thread, profile)
            if action == "send":
                conversation_service.send(
                    conversation=thread,
                    actor=request.user,
                    sender_kind=SenderKind.CANDIDATE,
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
                    side=SenderKind.CANDIDATE,
                    muted=thread.candidate_muted_at is None,
                )
                django_messages.success(
                    request,
                    "Muted. They can still write; you will not be notified."
                    if thread.candidate_muted_at
                    else "Unmuted.",
                )
            elif action == "block":
                conversation_service.block(
                    conversation=thread,
                    actor=request.user,
                    reason=request.POST.get("reason", ""),
                )
                django_messages.success(
                    request,
                    f"{thread.company.name} can no longer message you or see your profile.",
                )
                return redirect(reverse("messages:inbox"))
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
            restrictions.flash(request, exc)
            return redirect(back)

        return redirect(back)
