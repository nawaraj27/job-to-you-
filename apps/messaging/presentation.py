"""
How one thread reads to one viewer: attachment links and own-message controls.

Shared by both inboxes. Two defects made this its own module: attachments were
linked to `files:access` without a token, so every link 404ed; and editing and
deleting existed in the service with nothing on either screen to reach them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.files import policies as file_policies
from apps.files import signing

from .services.conversations import EDIT_WINDOW_MINUTES

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from apps.accounts.models import User

    from .models import Conversation, Message


@dataclass
class AttachmentRow:
    filename: str
    url: str = ""
    #: Why there is no link, when there is none.
    note: str = ""


@dataclass
class MessageRow:
    message: Message
    attachments: list[AttachmentRow] = field(default_factory=list)
    is_own: bool = False
    can_edit: bool = False
    can_delete: bool = False


def own_message_action(
    *, thread: Conversation, actor: User, action: str, message_id: str, body: str = ""
) -> str:
    """Edit or delete one of the actor's own messages in this thread (both inboxes).

    The message is looked up inside the thread already resolved for this
    viewer, so another thread's message id finds nothing; the service then
    refuses anything that is not the actor's own or is past the edit window.
    """
    from apps.common.errors import NotFound

    from .services import conversations as conversation_service

    raw = str(message_id or "")
    message = thread.messages.filter(pk=int(raw)).first() if raw.isdigit() else None
    if message is None:
        raise NotFound("That message is not in this conversation.")
    if action == "edit":
        conversation_service.edit(message=message, actor=actor, body=body)
        return "Message edited."
    conversation_service.delete(message=message, actor=actor)
    return "Message deleted. The other side sees that a message was deleted."


def thread_rows(messages: Iterable[Message], *, viewer: User) -> list[MessageRow]:
    """One row per message, with one-read links only to files this viewer may open."""
    import datetime as dt

    window = dt.timedelta(minutes=EDIT_WINDOW_MINUTES)
    now = timezone.now()
    rows = []
    for message in messages:
        is_own = message.sender_id == viewer.pk
        alive = message.deleted_at is None
        row = MessageRow(
            message=message,
            is_own=is_own,
            can_edit=is_own and alive and now - message.created_at <= window,
            can_delete=is_own and alive,
        )
        if alive:
            for attachment in message.attachments.all():
                if not attachment.is_servable:
                    note = (
                        "Being checked for viruses"
                        if attachment.scan_status == "pending"
                        else "Cannot be opened"
                    )
                    row.attachments.append(AttachmentRow(attachment.filename, note=note))
                elif file_policies.can_access(viewer, attachment):
                    url = signing.access_url(
                        attachment, actor_id=viewer.pk, purpose="message_attachment"
                    )
                    row.attachments.append(AttachmentRow(attachment.filename, url=url))
                else:
                    row.attachments.append(
                        AttachmentRow(attachment.filename, note="Shared with someone else")
                    )
        rows.append(row)
    return rows
