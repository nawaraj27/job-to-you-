"""
The record of what the taxonomy did not recognise (ADR-A46).

Resolution refuses to invent a skill per typo — that is what keeps "Python",
"python3" and "Pyhton" from becoming three requirements. The cost of that
refusal is that the term disappears, and the term is the only evidence of a
real gap. Fifty candidates typing "Kubeflow" is the strongest possible argument
for adding Kubeflow, and it is an argument nobody can reconstruct later: a term
nobody wrote down cannot be recovered from any other table.

So every failed resolution lands here, deduplicated by normalised form, counted
per channel, and queued for a curator. Recording never raises into the caller:
a profile save must not fail because a bookkeeping row could not be written.

    terms.record("Kubeflow", source=TermSource.PROFILE)
    terms.resolve(term=row, skill=kubeflow, actor=curator)   # adds the alias too
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import DatabaseError, transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState

from ..models import Skill, TermSource, TermStatus, UnresolvedTerm
from ..normalisation import normalise

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

logger = logging.getLogger(__name__)

#: Longer than this is a sentence, not a skill. Resume parsing will hand us
#: whole bullet points otherwise, and the queue becomes unreadable.
MAX_TERM_LENGTH = 120


def record(
    text: str,
    *,
    source: str = TermSource.OTHER,
    locale: str = "en",
    confidence: float | None = None,
) -> UnresolvedTerm | None:
    """Note that this term was not recognised. Returns None if it was ignorable.

    Never raises. The caller is in the middle of saving something the user
    cares about, and a ledger write failing must not take that with it.
    """
    key = normalise(text)
    if not key or len(key) > MAX_TERM_LENGTH:
        return None

    # Stored as Decimal so a caller passing a float does not leave the column
    # and the object disagreeing about the same number.
    score = Decimal(str(confidence)).quantize(Decimal("0.01")) if confidence is not None else None

    try:
        with transaction.atomic():
            row, created = UnresolvedTerm.objects.get_or_create(
                normalised=key,
                locale=locale,
                defaults={
                    "term": text.strip()[:200],
                    "source": source,
                    "source_counts": {source: 1},
                    "confidence": score,
                },
            )
            if not created:
                counts = dict(row.source_counts or {})
                counts[source] = counts.get(source, 0) + 1
                UnresolvedTerm.objects.filter(pk=row.pk).update(
                    occurrences=F("occurrences") + 1,
                    source=source,
                    source_counts=counts,
                    last_seen=timezone.now(),
                )
                row.refresh_from_db()
            return row
    except DatabaseError:
        logger.warning("unresolved term %r could not be recorded", text[:60], exc_info=True)
        return None


def pending(*, limit: int = 100) -> QuerySet[UnresolvedTerm]:
    """The curation queue: most-demanded gaps first."""
    return UnresolvedTerm.objects.filter(status=TermStatus.PENDING)[:limit]


@transaction.atomic
def resolve(
    *,
    term: UnresolvedTerm,
    skill: Skill,
    actor: User | None = None,
    add_alias: bool = True,
    note: str = "",
) -> UnresolvedTerm:
    """Map a term onto an existing skill.

    By default this also writes the alias, which is the entire point: resolving
    a term without teaching resolution about it leaves the next candidate who
    types it hitting the same wall, and the queue refilling with a row a
    curator has already answered.
    """
    if term.status == TermStatus.RESOLVED:
        raise InvalidState(f"{term.term!r} has already been resolved.")

    current = skill.resolve_current()

    if add_alias:
        from . import skills as skill_service

        # A term that already resolves elsewhere is left alone rather than
        # stolen — the curator is told, and the term is still marked resolved.
        try:
            skill_service.add_alias(
                skill=current,
                alias=term.term,
                locale=term.locale,
                actor=actor,
                reason=f"Resolved from the unknown-term queue{f': {note}' if note else ''}",
            )
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            note = f"{note} (alias not added: {exc})".strip()

    term.status = TermStatus.RESOLVED
    term.resolved_skill = current
    term.resolved_by = actor
    term.resolved_at = timezone.now()
    term.resolution_note = note[:200]
    term.save(
        update_fields=[
            "status",
            "resolved_skill",
            "resolved_by",
            "resolved_at",
            "resolution_note",
            "updated_at",
        ]
    )

    audit.record(
        verb="taxonomy.term_resolved",
        actor=actor,
        target=term,
        metadata={"term": term.term, "skill": current.slug},
    )
    return term


@transaction.atomic
def reject(*, term: UnresolvedTerm, actor: User | None = None, note: str = "") -> UnresolvedTerm:
    """Mark a term as not a skill.

    Kept rather than deleted: "asap", "team player" and "hard working" will
    arrive thousands of times, and a rejected row stops the same judgement
    being made again every week.
    """
    if term.status == TermStatus.RESOLVED:
        raise InvalidState("A resolved term cannot be rejected; reopen it first.")

    term.status = TermStatus.REJECTED
    term.resolved_by = actor
    term.resolved_at = timezone.now()
    term.resolution_note = note[:200]
    term.save(
        update_fields=["status", "resolved_by", "resolved_at", "resolution_note", "updated_at"]
    )
    audit.record(
        verb="taxonomy.term_rejected", actor=actor, target=term, metadata={"term": term.term}
    )
    return term


@transaction.atomic
def reopen(*, term: UnresolvedTerm, actor: User | None = None) -> UnresolvedTerm:
    """Put a term back in the queue after a mistaken decision."""
    if term.status == TermStatus.PENDING:
        raise InvalidState("That term is already in the queue.")

    term.status = TermStatus.PENDING
    term.resolved_skill = None
    term.resolved_at = None
    term.resolution_note = ""
    term.save(
        update_fields=[
            "status",
            "resolved_skill",
            "resolved_at",
            "resolution_note",
            "updated_at",
        ]
    )
    audit.record(verb="taxonomy.term_reopened", actor=actor, target=term)
    return term
