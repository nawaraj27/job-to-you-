"""
File reads (DEVELOPER.md §1.2).

Scoped by construction: there is no selector here that returns attachments
without an actor, because every private file read is somebody's read.
"""

from __future__ import annotations

from django.db.models import QuerySet

from .models import Attachment, AttachmentAccess


def for_actor(actor) -> QuerySet[Attachment]:
    """Attachments this actor may see listed.

    Not the same question as `policies.can_access` — that decides one file with
    a token in hand. This is the list view's scope.
    """
    queryset = Attachment.objects.alive()
    if actor is None or not actor.is_authenticated:
        return queryset.none()
    # `is_staff` is not authority here either (see `policies.can_access`).
    return queryset.filter(uploaded_by=actor)


def access_history(attachment: Attachment) -> QuerySet[AttachmentAccess]:
    """Who opened this file, newest first.

    The query behind "who viewed your CV" (SRS FR-2.8), which is why access is
    logged before the bytes are served rather than after.
    """
    return (
        AttachmentAccess.objects.filter(attachment=attachment)
        .select_related("actor")
        .order_by("-created_at")
    )


def by_public_id(public_id: str) -> Attachment | None:
    return Attachment.objects.alive().filter(public_id=public_id).first()
