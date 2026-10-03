"""
What a hiring team does to an application short of moving it: notes, tags,
ratings, mentions (FR-6.6, FR-6.11).

All of it is company-internal, permanently. There is no flag that could make a
note candidate-visible, and that absence is the feature: a product where a
recruiter's note *might* one day be surfaced is a product where nobody writes a
useful note.

Notes and ratings do not appear on the shared timeline for the same reason —
the timeline is the one stream both sides read (FR-6.4), and putting internal
commentary in it would mean filtering internal rows out of a candidate view
forever, which is one `visible_to_candidate=False` away from a leak.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState, NotFound

from ..models import (
    Application,
    ApplicationNote,
    ApplicationRating,
    ApplicationTag,
    CompanyCandidateNote,
    Tag,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company, CompanyMembership

MAX_NOTE_LENGTH = 5000
MAX_TAGS_PER_COMPANY = 60


@transaction.atomic
def add_note(
    *,
    application: Application,
    actor: User,
    membership: CompanyMembership,
    body: str,
    mentions: list[int] | None = None,
) -> ApplicationNote:
    text = (body or "").strip()
    if not text:
        raise InvalidState("Write something first.")

    note = ApplicationNote.objects.create(
        application=application,
        author=actor,
        body=text[:MAX_NOTE_LENGTH],
        mentions=_clean_mentions(membership, mentions),
    )
    application.last_activity_at = timezone.now()
    application.save(update_fields=["last_activity_at", "updated_at"])

    audit.record(
        verb="application.note_added",
        actor=actor,
        target=application,
        company=application.job.company,
    )
    return note


@transaction.atomic
def delete_note(*, note: ApplicationNote, actor: User) -> None:
    """Soft, and only by its author.

    An admin who could delete a colleague's note could rewrite the record of a
    hiring decision, which is precisely what these rows exist to preserve.
    """
    if note.author_id != actor.pk:
        from apps.common.errors import NotAllowed

        raise NotAllowed("You can only delete your own notes.")

    note.deleted_at = timezone.now()
    note.save(update_fields=["deleted_at", "updated_at"])
    audit.record(
        verb="application.note_deleted",
        actor=actor,
        target=note.application,
        company=note.application.job.company,
    )


def _clean_mentions(membership: CompanyMembership, mentions: list[int] | None) -> list[int]:
    """Only teammates. A mention of somebody outside the company is a mention
    of a person who cannot open the page it points at."""
    if not mentions:
        return []

    from apps.companies.models import CompanyMembership as Membership

    valid = Membership.objects.active().filter(
        company=membership.company, pk__in=[int(item) for item in mentions if str(item).isdigit()]
    )
    return list(valid.values_list("pk", flat=True))


# --- tags -----------------------------------------------------------------


@transaction.atomic
def add_tag(
    *, application: Application, actor: User, company: Company, name: str, colour: str = ""
) -> ApplicationTag:
    label = (name or "").strip()[:40]
    if not label:
        raise InvalidState("A tag needs a name.")

    tag = Tag.objects.filter(company=company, name__iexact=label).first()
    if tag is None:
        if Tag.objects.filter(company=company).count() >= MAX_TAGS_PER_COMPANY:
            raise InvalidState(
                f"A company keeps up to {MAX_TAGS_PER_COMPANY} tags. "
                "More than that and they stop being a filter."
            )
        tag = Tag.objects.create(company=company, name=label, colour=colour.strip()[:20])

    link, created = ApplicationTag.objects.get_or_create(
        application=application, tag=tag, defaults={"added_by": actor}
    )
    if created:
        audit.record(
            verb="application.tagged",
            actor=actor,
            target=application,
            company=company,
            metadata={"tag": tag.name},
        )
    return link


@transaction.atomic
def remove_tag(*, application: Application, actor: User, tag_id: int) -> None:
    removed, _ = application.tag_links.filter(tag_id=tag_id).delete()
    if not removed:
        raise NotFound("That tag is not on this application.")
    audit.record(
        verb="application.untagged",
        actor=actor,
        target=application,
        company=application.job.company,
    )


@transaction.atomic
def tag_many(*, applications: list[Application], actor: User, company: Company, name: str) -> int:
    """Bulk tag (FR-6.6)."""
    tagged = 0
    for application in applications:
        add_tag(application=application, actor=actor, company=company, name=name)
        tagged += 1
    return tagged


# --- ratings --------------------------------------------------------------


@transaction.atomic
def rate(
    *, application: Application, actor: User, score: int, comment: str = ""
) -> ApplicationRating:
    """One score per rater, updated rather than duplicated.

    A panel is meant to produce a spread of opinions; a single shared field
    produces an argument over who wrote last.
    """
    try:
        value = int(score)
    except (TypeError, ValueError):
        raise InvalidState("A rating is a number from 1 to 5.") from None
    if not 1 <= value <= 5:
        raise InvalidState("A rating is a number from 1 to 5.")

    rating, _ = ApplicationRating.objects.update_or_create(
        application=application,
        rater=actor,
        defaults={"score": value, "comment": (comment or "").strip()[:300]},
    )
    audit.record(
        verb="application.rated",
        actor=actor,
        target=application,
        company=application.job.company,
        metadata={"score": value},
    )
    return rating


# --- notes on a candidate with no application -----------------------------


@transaction.atomic
def add_candidate_note(
    *, company: Company, candidate: CandidateProfile, actor: User, body: str
) -> CompanyCandidateNote:
    """A note about a sourced person (SRS §5.8).

    Requires a live visibility grant like every other employer-side read: a
    note is a record about somebody, and writing one about a person the company
    has no basis to see is the same problem as reading them.
    """
    from . import visibility

    visibility.require_access(candidate=candidate, company=company)

    text = (body or "").strip()
    if not text:
        raise InvalidState("Write something first.")

    note = CompanyCandidateNote.objects.create(
        company=company, candidate=candidate, author=actor, body=text[:MAX_NOTE_LENGTH]
    )
    audit.record(verb="candidate.note_added", actor=actor, target=candidate, company=company)
    return note
