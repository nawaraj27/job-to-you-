"""
Import proposals: what a parse offers, and what an accept does (ADR-A49).

A parse never writes to the profile. It writes proposals, and an explicit
accept calls the same candidate service the profile screen calls — so an
imported skill goes through taxonomy resolution exactly like a typed one, and
an imported role gets the same date validation.

Two things this deliberately does not do:

  * **It does not resolve skills itself.** It proposes the *terms* it saw.
    `candidates.services.skills.add_skill` resolves them, and an unresolvable
    term lands in the unknown-term ledger (ADR-A46) like any other.
  * **It does not overwrite.** A proposal whose value is already on the profile
    is not offered, and accepting one that has since become a duplicate reports
    that rather than failing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import DomainError, InvalidState

from ..models import (
    ProposalKind,
    ProposalState,
    ResumeImportProposal,
    ResumeParse,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile

logger = logging.getLogger(__name__)

#: What applying one proposal looks like: it does the work and says what it did.
Handler = Callable[["ResumeImportProposal", "User"], str]

#: Proposing eighty skills from a keyword-stuffed CV is not help. The top of
#: the list is where the real ones are.
MAX_SKILL_PROPOSALS = 25
MAX_ROW_PROPOSALS = 15


@transaction.atomic
def build_from_parse(
    *, parse: ResumeParse, profile: CandidateProfile
) -> list[ResumeImportProposal]:
    """Turn one parse into the offers a candidate can accept.

    Re-parsing supersedes the previous offers rather than deleting them: a
    proposal someone already rejected must not come back looking new.

    Superseding is matched on the **upload**, not on the parse row. A parse is
    a one-to-one on its upload and re-reading updates that row in place, so
    "the offers from a different parse of this upload" is a set that is always
    empty — the guard that was meant to replace the old offers excluded every
    one of them, and re-reading a file left both generations on screen. A
    candidate whose CV had been read twice saw every skill, every degree and
    their telephone number offered twice over, and accepting one copy left the
    other sitting there.
    """
    ResumeImportProposal.objects.filter(
        profile=profile, parse__upload=parse.upload, state=ProposalState.OFFERED
    ).update(state=ProposalState.SUPERSEDED, decided_at=timezone.now())

    structured = parse.structured or {}
    confidence = {key: Decimal(value) for key, value in (parse.confidence or {}).items()}
    proposals: list[ResumeImportProposal] = []

    standing = _standing_offers(profile)

    proposals += _skill_proposals(parse, profile, structured, confidence, standing)
    proposals += _experience_proposals(parse, profile, structured, confidence, standing)
    proposals += _education_proposals(parse, profile, structured, confidence, standing)
    proposals += _contact_proposals(parse, profile, structured, confidence, standing)

    if proposals:
        audit.record(
            verb="resume.proposals_built",
            target=profile,
            metadata={"count": len(proposals), "upload": str(parse.upload.public_id)},
        )
    return proposals


def accept(*, proposal: ResumeImportProposal, actor: User) -> ResumeImportProposal:
    """Apply one proposal through the service that owns the field.

    Not wrapped in one transaction: the services called below manage their own,
    and an unresolvable skill term must reach the unknown-term ledger even
    though the accept that triggered it fails (ADR-A46). A single atomic block
    would roll the ledger row back with the refusal.
    """
    if not proposal.is_open:
        raise InvalidState("That suggestion has already been decided.")

    handlers: dict[str, Handler] = {
        ProposalKind.SKILL: _accept_skill,
        ProposalKind.EXPERIENCE: _accept_experience,
        ProposalKind.EDUCATION: _accept_education,
        ProposalKind.CONTACT: _accept_contact,
        ProposalKind.HEADLINE: _accept_headline,
    }
    handler = handlers.get(proposal.kind)

    if handler is None:
        raise InvalidState("That suggestion cannot be applied.")

    outcome = handler(proposal, actor)

    with transaction.atomic():
        proposal.state = ProposalState.ACCEPTED
        proposal.decided_at = timezone.now()
        proposal.decided_by = actor
        proposal.outcome = outcome[:200]
        proposal.save(update_fields=["state", "decided_at", "decided_by", "outcome", "updated_at"])

    audit.record(
        verb="resume.proposal_accepted",
        actor=actor,
        target=proposal.profile,
        metadata={"kind": proposal.kind, "label": proposal.label},
    )
    return proposal


@transaction.atomic
def reject(*, proposal: ResumeImportProposal, actor: User) -> ResumeImportProposal:
    if not proposal.is_open:
        raise InvalidState("That suggestion has already been decided.")

    proposal.state = ProposalState.REJECTED
    proposal.decided_at = timezone.now()
    proposal.decided_by = actor
    proposal.save(update_fields=["state", "decided_at", "decided_by", "updated_at"])

    audit.record(
        verb="resume.proposal_rejected",
        actor=actor,
        target=proposal.profile,
        metadata={"kind": proposal.kind, "label": proposal.label},
    )
    return proposal


def accept_all(
    *, profile: CandidateProfile, actor: User, kind: str | None = None
) -> dict[str, int]:
    """Accept every open proposal, reporting what each one did.

    Failures are counted, not raised: one role with an unreadable date must not
    stop the other fourteen from being imported.
    """
    queryset = ResumeImportProposal.objects.filter(profile=profile, state=ProposalState.OFFERED)
    if kind:
        queryset = queryset.filter(kind=kind)

    applied = failed = 0
    for proposal in queryset.select_related("parse"):
        try:
            accept(proposal=proposal, actor=actor)
            applied += 1
        except DomainError as exc:
            proposal.state = ProposalState.REJECTED
            proposal.decided_at = timezone.now()
            proposal.decided_by = actor
            proposal.outcome = f"Not applied: {exc.message}"[:200]
            proposal.save(
                update_fields=["state", "decided_at", "decided_by", "outcome", "updated_at"]
            )
            failed += 1

    return {"applied": applied, "failed": failed}


# --- building -------------------------------------------------------------


def identity(kind: str, label: str, payload: dict) -> tuple:
    """What makes two offers the same offer.

    Not the primary key and not the label: the same fact read out of two
    different files is one thing the candidate has to decide about, however the
    two documents happened to spell it.
    """
    payload = payload or {}
    if kind == ProposalKind.SKILL:
        return (kind, (payload.get("resolved_slug") or payload.get("term") or label).casefold())
    if kind == ProposalKind.EXPERIENCE:
        return (
            kind,
            (payload.get("title") or "").casefold(),
            (payload.get("org_name") or "").casefold(),
        )
    if kind == ProposalKind.EDUCATION:
        return (
            kind,
            (payload.get("qualification") or "").casefold(),
            (payload.get("institution") or "").casefold(),
        )
    if kind == ProposalKind.CONTACT:
        return (kind, "".join(c for c in (payload.get("phone") or label) if c.isdigit()))
    return (kind, label.casefold())


def _standing_offers(profile: CandidateProfile) -> set[tuple]:
    """Everything this candidate has already been offered and not yet decided.

    A second CV repeats most of the first one — the same phone number, the same
    degree, the same eight skills. Without this, uploading it offered every one
    of them a second time, and the import list a candidate with three CVs on
    file opened was three near-identical copies of the same twenty decisions.

    This upload's own previous offers are already superseded by the time this
    runs, so re-reading one file replaces its offers rather than suppressing
    them.
    """
    rows = ResumeImportProposal.objects.filter(profile=profile, state=ProposalState.OFFERED)
    return {identity(row.kind, row.label, row.payload) for row in rows}


def _new(standing: set[tuple], row: ResumeImportProposal) -> bool:
    """Whether this offer is one the candidate has not already got."""
    key = identity(row.kind, row.label, row.payload)
    if key in standing:
        return False
    standing.add(key)
    return True


def _skill_proposals(
    parse: ResumeParse,
    profile: CandidateProfile,
    structured: dict,
    confidence: dict[str, Decimal],
    standing: set[tuple],
) -> list[ResumeImportProposal]:
    from apps.candidates.models import CandidateSkill
    from apps.taxonomy.services import skills as resolver

    claimed = set(CandidateSkill.objects.filter(profile=profile).values_list("skill_id", flat=True))
    rows = []

    for order, term in enumerate(structured.get("skills", [])[:MAX_SKILL_PROPOSALS]):
        match = resolver.resolve(term)
        if match is not None and match.skill.pk in claimed:
            # Already on the profile. Offering it again is noise the candidate
            # has to read and dismiss.
            continue

        rows.append(
            ResumeImportProposal(
                parse=parse,
                profile=profile,
                kind=ProposalKind.SKILL,
                label=match.skill.name if match else term,
                payload={"term": term, "resolved_slug": match.skill.slug if match else ""},
                confidence=confidence.get("skills"),
                evidence=term[:300],
                order=order,
            )
        )

    return ResumeImportProposal.objects.bulk_create([row for row in rows if _new(standing, row)])


def _experience_proposals(
    parse: ResumeParse,
    profile: CandidateProfile,
    structured: dict,
    confidence: dict[str, Decimal],
    standing: set[tuple],
) -> list[ResumeImportProposal]:
    existing = {
        (row.title.casefold(), row.org_name.casefold()) for row in profile.experiences.all()
    }
    rows = []

    for order, entry in enumerate(structured.get("experience", [])[:MAX_ROW_PROPOSALS]):
        title = (entry.get("title") or "").strip()
        org = (entry.get("org") or "").strip()
        if not title or (title.casefold(), org.casefold()) in existing:
            continue

        rows.append(
            ResumeImportProposal(
                parse=parse,
                profile=profile,
                kind=ProposalKind.EXPERIENCE,
                label=f"{title}{f' at {org}' if org else ''}",
                payload={
                    "title": title,
                    "org_name": org or title,
                    "start_date": _as_date(entry.get("start")),
                    "end_date": None if entry.get("is_current") else _as_date(entry.get("end")),
                    "is_current": bool(entry.get("is_current")),
                    "summary": " ".join(entry.get("bullets", []))[:2000],
                },
                confidence=confidence.get("experience"),
                evidence=(entry.get("title") or "")[:300],
                order=order,
            )
        )

    return ResumeImportProposal.objects.bulk_create([row for row in rows if _new(standing, row)])


def _education_proposals(
    parse: ResumeParse,
    profile: CandidateProfile,
    structured: dict,
    confidence: dict[str, Decimal],
    standing: set[tuple],
) -> list[ResumeImportProposal]:
    existing = {row.institution.casefold() for row in profile.education.all()}
    rows = []

    for order, entry in enumerate(structured.get("education", [])[:MAX_ROW_PROPOSALS]):
        institution = (entry.get("institution") or "").strip()
        qualification = (entry.get("qualification") or "").strip()
        if not (institution or qualification) or institution.casefold() in existing:
            continue

        rows.append(
            ResumeImportProposal(
                parse=parse,
                profile=profile,
                kind=ProposalKind.EDUCATION,
                label=" — ".join(part for part in (qualification, institution) if part),
                payload={
                    "institution": institution or qualification,
                    "qualification": qualification,
                    "start_date": _as_date(entry.get("start")),
                    "end_date": _as_date(entry.get("end")),
                },
                confidence=confidence.get("education"),
                evidence=(entry.get("institution") or "")[:300],
                order=order,
            )
        )

    return ResumeImportProposal.objects.bulk_create([row for row in rows if _new(standing, row)])


def _contact_proposals(
    parse: ResumeParse,
    profile: CandidateProfile,
    structured: dict,
    confidence: dict[str, Decimal],
    standing: set[tuple],
) -> list[ResumeImportProposal]:
    rows = []
    contact = structured.get("contact", {})

    phone = next(iter(contact.get("phones", [])), "")
    if phone and not profile.phone:
        rows.append(
            ResumeImportProposal(
                parse=parse,
                profile=profile,
                kind=ProposalKind.CONTACT,
                label=f"Phone: {phone}",
                payload={"phone": phone},
                confidence=confidence.get("contact"),
                evidence=phone[:300],
            )
        )

    headline = structured.get("headline", "")
    if headline and not profile.headline:
        rows.append(
            ResumeImportProposal(
                parse=parse,
                profile=profile,
                kind=ProposalKind.HEADLINE,
                label=headline,
                payload={"headline": headline},
                confidence=Decimal("0.40"),
                evidence=headline[:300],
            )
        )

    # Email is never proposed: it is the account's identity, changed through
    # the verified-email flow, not by a line in a document.
    return ResumeImportProposal.objects.bulk_create([row for row in rows if _new(standing, row)])


# --- accepting ------------------------------------------------------------


def _accept_skill(proposal: ResumeImportProposal, actor: User) -> str:
    from apps.candidates.services import skills as candidate_skills

    term = proposal.payload.get("term") or proposal.label
    record = candidate_skills.add_skill(
        profile=proposal.profile,
        actor=actor,
        text=term,
        source="resume",
    )
    return f"Added {record.skill.name}."


def _accept_experience(proposal: ResumeImportProposal, actor: User) -> str:
    from apps.candidates.services import sections

    payload = dict(proposal.payload)
    if not payload.get("start_date"):
        # The profile requires a start date and the parser could not find one.
        # Refused with a message the candidate can act on, rather than invented.
        raise InvalidState(
            "We could not read a start date for that role. Add it from your profile instead."
        )

    sections.add_entry(profile=proposal.profile, actor=actor, section="experience", **payload)
    return f"Added {proposal.label}."


def _accept_education(proposal: ResumeImportProposal, actor: User) -> str:
    from apps.candidates.services import sections

    sections.add_entry(
        profile=proposal.profile, actor=actor, section="education", **dict(proposal.payload)
    )
    return f"Added {proposal.label}."


def _accept_contact(proposal: ResumeImportProposal, actor: User) -> str:
    from apps.candidates.services import profile as profile_service

    profile_service.update_basics(
        profile=proposal.profile, actor=actor, phone=proposal.payload.get("phone", "")
    )
    return "Phone number updated."


def _accept_headline(proposal: ResumeImportProposal, actor: User) -> str:
    from apps.candidates.services import profile as profile_service

    profile_service.update_basics(
        profile=proposal.profile, actor=actor, headline=proposal.payload.get("headline", "")
    )
    return "Headline updated."


def _as_date(value: str | None) -> str | None:
    """Turn a parsed date fragment into an ISO date the section service takes.

    A CV says "Aug 2023"; the profile stores a date. The first of the month is
    the honest reading of a month-precision date, and it is what every CV
    importer does — stated here so nobody later reads it as a bug.
    """
    if not value:
        return None

    import re

    text = value.strip().casefold()
    year = re.search(r"(19|20)\d{2}", text)
    if year is None:
        return None

    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    month = next((number for prefix, number in months.items() if prefix in text), 1)
    return f"{year.group(0)}-{month:02d}-01"
