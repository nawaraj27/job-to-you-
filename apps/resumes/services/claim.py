"""
Claiming a guest upload, and sweeping the ones nobody claims (ADR-A32).

Claiming is the moment processing becomes permitted. Before it, the platform
holds bytes; after it, an account has said "this is mine, read it". The two
states are not a formality — they are the difference between storing a file and
processing a stranger's personal data, and the transition is the only place
where `parse_status` may leave `blocked`.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.files import services as file_services
from apps.files.constants import ScanStatus
from apps.notifications import events

from ..models import ParseStatus, ResumeUpload
from .upload import GUEST_RETENTION_HOURS, find_guest_upload

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile


@transaction.atomic
def claim(*, token: str, profile: CandidateProfile, actor: User) -> ResumeUpload:
    """Attach an unclaimed upload to a profile, and only then allow parsing."""
    upload = find_guest_upload(token)
    if upload is None:
        raise NotFound("That upload has expired or was already claimed. Upload the file again.")

    duplicate = ResumeUpload.objects.filter(profile=profile, sha256=upload.sha256).first()
    if duplicate is not None:
        # They already have this CV. The guest copy is dropped rather than
        # kept as a second row that would parse to the same proposals twice.
        _discard(upload, actor=actor, reason="already on the profile")
        return duplicate

    upload.profile = profile
    upload.guest_token = ""
    upload.claimed_at = timezone.now()
    upload.parse_status = ParseStatus.PENDING
    upload.save(
        update_fields=["profile", "guest_token", "claimed_at", "parse_status", "updated_at"]
    )

    events.emit(
        "resume.claimed",
        upload,
        payload={
            "upload_public_id": str(upload.public_id),
            "profile_public_id": str(profile.public_id),
        },
    )
    audit.record(
        verb="resume.claimed",
        actor=actor,
        target=upload,
        metadata={"filename": upload.original_filename},
    )

    if upload.attachment.scan_status == ScanStatus.CLEAN:
        upload_id = upload.pk
        transaction.on_commit(lambda: _enqueue(upload_id))

    return upload


@transaction.atomic
def claim_all_for(
    *, tokens: list[str], profile: CandidateProfile, actor: User
) -> list[ResumeUpload]:
    """Claim everything a newly registered account uploaded as a guest."""
    claimed = []
    for token in tokens:
        try:
            claimed.append(claim(token=token, profile=profile, actor=actor))
        except (NotFound, Conflict, InvalidState):
            # One expired token must not cost the others. The candidate is told
            # how many arrived, which is the only number they can act on.
            continue
    return claimed


def sweep_unclaimed(*, older_than_hours: int = GUEST_RETENTION_HOURS) -> int:
    """Delete uploads nobody claimed. Returns how many went.

    Not a soft delete: the platform cannot identify who these belong to, so
    keeping them is holding personal data with no owner, no lawful basis and
    nobody to ask.
    """
    cutoff = timezone.now() - dt.timedelta(hours=older_than_hours)
    stale = ResumeUpload.objects.unclaimed().filter(created_at__lt=cutoff)

    removed = 0
    for upload in stale.select_related("attachment"):
        _discard(upload, actor=None, reason="unclaimed past retention")
        removed += 1

    if removed:
        audit.record(
            verb="resume.unclaimed_swept",
            metadata={"removed": removed, "older_than_hours": older_than_hours},
        )
    return removed


def _discard(upload: ResumeUpload, *, actor: User | None, reason: str) -> None:
    attachment = upload.attachment
    upload.delete()
    file_services.soft_delete(attachment=attachment, actor=actor)


def _enqueue(upload_id: int) -> None:
    from ..tasks import parse_upload

    parse_upload.delay(upload_id)
