"""
Profile ↔ document reconciliation (ADR-A7, INV-7, SRS §5.5.3).

Provenance alone is not a protocol. Each class of field has one canonical
owner, and drift is a state the interface can render:

    Facts    — employer, dates, titles, institutions   canon: CandidateProfile
    Wording  — summary, bullets, ordering, emphasis    canon: the document
    Skills   — canon: CandidateSkill

So this module never writes to either side by itself. It returns a **diff**,
and the two directions are deliberately asymmetric:

  * profile → document is offered as a `ResumeSuggestion`, never applied
    silently, because a document's wording is the candidate's own;
  * document → profile is only ever an import proposal (P4, ADR-A49), because
    the profile is what matching reads.

`linked` entries follow the profile. `edited` ones have diverged and are
reported as drift. `detached` ones are document-only by choice and are never
mentioned again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import transaction

from .. import content as content_rules
from ..models import ResumeDocument
from . import suggestions as suggestion_service

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile


@dataclass(frozen=True)
class Drift:
    """One difference between the profile and the document."""

    kind: str  # experience | education | skill | contact
    direction: str  # profile_to_document | document_to_profile
    label: str
    detail: str = ""
    entry_id: str = ""
    target_path: str = ""
    proposed: str = ""
    current: str = ""


@dataclass
class Reconciliation:
    """What the interface renders with accept / keep-mine."""

    document_public_id: str
    profile_to_document: list[Drift] = field(default_factory=list)
    document_to_profile: list[Drift] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.profile_to_document or self.document_to_profile)

    @property
    def count(self) -> int:
        return len(self.profile_to_document) + len(self.document_to_profile)


def reconcile(document: ResumeDocument) -> Reconciliation:
    """Compare a document against its owner's profile. Writes nothing."""
    profile = document.profile
    content = document.draft.content

    return Reconciliation(
        document_public_id=str(document.public_id),
        profile_to_document=[
            *_linked_entry_drift(profile, content),
            *_contact_drift(profile, content),
        ],
        document_to_profile=_skills_missing_from_profile(profile, content),
    )


@transaction.atomic
def offer_suggestions(*, document: ResumeDocument, actor: User | None = None) -> int:
    """Turn profile → document drift into suggestions the candidate can accept.

    Only that direction. A document's skills becoming profile skills goes
    through P4's import proposals, because the profile is canon for skills and
    the two mechanisms must not both claim it.
    """
    result = reconcile(document)
    draft = document.draft
    offered = 0

    for drift in result.profile_to_document:
        if not drift.target_path or not drift.proposed:
            continue
        suggestion_service.offer(
            draft=draft,
            target_path=drift.target_path,
            proposed=drift.proposed,
            original=drift.current,
            source="reconcile",
            rationale=drift.detail[:200],
        )
        offered += 1

    return offered


# --- profile → document ---------------------------------------------------


def _linked_entry_drift(profile: CandidateProfile, content: dict) -> list[Drift]:
    """Entries that came from the profile and no longer match it.

    Only `linked` entries are compared. An `edited` entry is the candidate's
    wording and an offer to overwrite it would be the platform arguing with
    them; a `detached` one never claimed to come from the profile at all.
    """
    experiences = {str(row.pk): row for row in profile.experiences.all()}
    drift: list[Drift] = []

    for section in content.get("sections", []):
        for entry in section.get("entries", []) or []:
            if entry.get("sync_state") != content_rules.SyncState.LINKED:
                continue

            reference = (entry.get("source_ref") or "").split(":")
            if len(reference) != 2 or reference[0] != "experience":
                continue

            row = experiences.get(reference[1])
            if row is None:
                drift.append(
                    Drift(
                        kind="experience",
                        direction="profile_to_document",
                        label=entry.get("title", "") or "An entry",
                        detail="This role is no longer on your profile.",
                        entry_id=entry.get("id", ""),
                    )
                )
                continue

            if (entry.get("title") or "") != row.title:
                drift.append(
                    Drift(
                        kind="experience",
                        direction="profile_to_document",
                        label=row.title,
                        detail="Your profile has a different job title.",
                        entry_id=entry.get("id", ""),
                        target_path=f"entry.{entry.get('id')}.title",
                        proposed=row.title,
                        current=entry.get("title", ""),
                    )
                )

            if (entry.get("org") or "") != row.org_name:
                drift.append(
                    Drift(
                        kind="experience",
                        direction="profile_to_document",
                        label=row.org_name,
                        detail="Your profile has a different employer.",
                        entry_id=entry.get("id", ""),
                        target_path=f"entry.{entry.get('id')}.org",
                        proposed=row.org_name,
                        current=entry.get("org", ""),
                    )
                )

    return drift


def _contact_drift(profile: CandidateProfile, content: dict) -> list[Drift]:
    """Contact details the profile knows and the document does not.

    Facts are the profile's to own, but the offer is still an offer: a CV
    deliberately without a phone number is a real choice, and overwriting it
    would be the platform undoing a decision.
    """
    contact = content.get("contact", {})
    drift = []

    if profile.headline and contact.get("headline", "") != profile.headline:
        drift.append(
            Drift(
                kind="contact",
                direction="profile_to_document",
                label="Current title",
                detail="Your profile says something different.",
                target_path="contact.headline",
                proposed=profile.headline,
                current=contact.get("headline", ""),
            )
        )

    if profile.phone and not contact.get("phone"):
        drift.append(
            Drift(
                kind="contact",
                direction="profile_to_document",
                label="Phone number",
                detail="Your profile has a phone number this CV does not show.",
                target_path="contact.phone",
                proposed=profile.phone,
                current="",
            )
        )

    return drift


# --- document → profile ---------------------------------------------------


def _skills_missing_from_profile(profile: CandidateProfile, content: dict) -> list[Drift]:
    """Skills on the CV that the profile does not claim.

    Reported, not applied: `CandidateSkill` is canon, and adding to it goes
    through taxonomy resolution and the candidate's own accept (ADR-A49). This
    is the "two skills on your CV are not on your profile" line the dashboard
    shows.
    """
    from apps.taxonomy.services import skills as resolver

    section = content_rules.section_by_key(content, "skills")
    if section is None:
        return []

    claimed = set(profile.skills.values_list("skill_id", flat=True))
    drift: list[Drift] = []
    seen: set[int] = set()

    # Resolved as a list rather than a term at a time: this runs on every
    # render of all three builder screens, and one lookup per skill put them
    # over NFR-2's twenty-five queries on any CV with a real skills section.
    terms = list(section.get("items", []) or [])
    resolved = resolver.resolve_many(terms)

    for term in terms:
        match = resolved.get(term)
        if match is None or match.skill.pk in claimed or match.skill.pk in seen:
            continue
        seen.add(match.skill.pk)
        drift.append(
            Drift(
                kind="skill",
                direction="document_to_profile",
                label=match.skill.name,
                detail="On your CV but not on your profile, so job matching cannot see it.",
                proposed=match.skill.name,
            )
        )

    return drift
