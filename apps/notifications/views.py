"""
Notifications, on real rows (P10).

One list screen and one command endpoint — the same shape as the mock this
replaces, because the shape was right. What changed is underneath: the feed is
`Notification.objects.for_user(...)` rather than a session store, and muting
filters the query rather than a list in memory.

Signed in only. A notification belongs to a person; there is no anonymous feed
to render and nothing sensible to show if there were.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from apps.common.pagination import page_of

from .services import feed as feed_service

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

PAGE_SIZE = 20


def _respond(request, template, context, message=None, error=None):
    context["flash_error"] = error
    response = render(request, template, context)
    announce = error or message
    if announce:
        response["HX-Trigger"] = json.dumps({"builder:announce": announce})
    return response


def _intro(unread: int, total: int) -> str:
    """The state of the list in a sentence, since the counts are already chips."""
    if not total:
        return "Nothing has arrived yet."
    if not unread:
        return "You're up to date. Everything below has been read."
    if unread == 1:
        return "One thing you haven't read yet."
    return f"{unread} things you haven't read yet."


def _user(request: HttpRequest) -> User:
    """The signed-in user, narrowed.

    Every view here is behind `login_required`, so this cannot be anonymous —
    but the request type does not know that, and asserting it once beats an
    ignore comment at each of six call sites.
    """
    return cast("User", request.user)


def feed_context(request: HttpRequest) -> dict:
    user = _user(request)
    kind = request.GET.get("kind", "") or request.POST.get("kind", "")
    unread_only = (request.GET.get("unread") or request.POST.get("unread")) == "1"

    queryset = feed_service.feed(user)
    if kind:
        queryset = queryset.filter(kind=kind)
    if unread_only:
        queryset = queryset.filter(read_at__isnull=True)

    page = page_of(
        queryset,
        number=request.GET.get("page") or request.POST.get("page"),
        per_page=PAGE_SIZE,
    )
    unread = feed_service.unread_count(user)

    return {
        "page_title": "Notifications",
        "intro": _intro(unread, page["count"]),
        "kind": kind,
        "unread_only": unread_only,
        # `order_by("kind")` before `distinct()`, not after. The feed is
        # ordered by arrival, and `DISTINCT` in Postgres considers every column
        # in the ORDER BY — including the timestamp — so the same kind came
        # back once per notification and the filter row showed "Application
        # stage changed" three times.
        "kinds": sorted(
            feed_service.feed(user).order_by("kind").values_list("kind", flat=True).distinct()
        ),
        "undo": feed_service.last_dismissed(user),
        "page": page,
        "items": page["items"],
        "unread": unread,
        "total": page["count"],
    }


@method_decorator(login_required, name="dispatch")
class NotificationsView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "notifications/index.html", feed_context(request))


@method_decorator(login_required, name="dispatch")
class OpenView(View):
    """Follow a notification: mark it read, then go where it points.

    A redirect rather than a link straight to the target, so opening something
    from the bell clears it — the commonest complaint about notification lists
    is having to mark as read what you have visibly just read.
    """

    def get(self, request: HttpRequest, public_id: str) -> HttpResponse:
        notification = feed_service.mark_read(user=_user(request), public_id=public_id)
        if notification is None or not notification.target_url:
            return redirect("notifications:index")
        return redirect(notification.target_url)


@method_decorator(login_required, name="dispatch")
class NotificationCommandView(View):
    """read · dismiss · undo · read_all"""

    def post(self, request: HttpRequest) -> HttpResponse:
        command = request.POST.get("command", "")
        public_id = request.POST.get("id", "")
        user = _user(request)
        message = error = None

        if command == "page":
            # Moving through the list changes nothing; feed_context has already
            # read the page number out of the request.
            pass

        elif command == "read":
            if feed_service.mark_read(user=user, public_id=public_id) is None:
                error = "That notification is gone."
            else:
                message = "Marked as read"

        elif command == "dismiss":
            dismissed = feed_service.dismiss(user=user, public_id=public_id)
            if dismissed is None:
                error = "That notification is gone."
            else:
                message = f"Dismissed “{dismissed.title}”. Undo is available."

        elif command == "undo":
            restored = feed_service.undismiss(user=user, public_id=public_id)
            if restored is None:
                error = "There is nothing to restore."
            else:
                message = f"Restored “{restored.title}”"

        elif command == "read_all":
            changed = feed_service.mark_all_read(user=user, kind=request.POST.get("kind", ""))
            message = f"{changed} marked as read" if changed else "Everything was already read"

        else:
            return HttpResponse("Unknown command: " + repr(command), status=400)

        return _respond(request, "notifications/_feed.html", feed_context(request), message, error)
