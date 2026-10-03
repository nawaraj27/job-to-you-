"""Company lifecycle."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.models import User
from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotAllowed, NotFound, StaleWrite
from apps.common.state_machine import StateMachine, Transition
from apps.files import constants as file_constants
from apps.files import services as file_service
from apps.notifications import events

from .. import policies
from ..middleware import RESERVED_SLUGS
from ..models import (
    VERIFICATION_DOCUMENT_KINDS,
    Company,
    CompanyInvite,
    CompanyMembership,
    CompanyState,
    CompanyVerification,
    CompanyVerificationDecision,
    CompanyVerificationDocument,
    VerificationState,
)
from ..permissions import Role

#: The verification state machine (SRS §7.5). The middle states are the point:
#: a product that only knows "verified / not" has nothing to show an employer on
#: the day they are waiting, which is the day they look hardest.
VERIFICATION = StateMachine(
    "state",
    transitions=[
        Transition(VerificationState.NOT_SUBMITTED, VerificationState.PENDING),
        Transition(VerificationState.PENDING, VerificationState.MORE_INFO),
        Transition(VerificationState.PENDING, VerificationState.VERIFIED),
        Transition(VerificationState.PENDING, VerificationState.REJECTED),
        Transition(VerificationState.MORE_INFO, VerificationState.PENDING),
        Transition(VerificationState.VERIFIED, VerificationState.PENDING),
        # A new attempt after a rejection (decision D-02). The machine allows
        # the edge; `submit_verification` enforces the waiting period.
        Transition(VerificationState.REJECTED, VerificationState.PENDING),
    ],
)

#: How long after a rejection a company may apply again (decision D-02). Long
#: enough that a rejection is not answered with the same papers the next
#: morning, short enough that a company that fixed the problem is not shut out.
REAPPLY_AFTER_DAYS = 30


def reapply_opens_at(verification: CompanyVerification) -> dt.datetime | None:
    """When a rejected company may submit again, or None if it is not rejected."""
    if verification.state != VerificationState.REJECTED or verification.reviewed_at is None:
        return None
    return verification.reviewed_at + dt.timedelta(days=REAPPLY_AFTER_DAYS)


def can_reapply(verification: CompanyVerification, *, now: dt.datetime | None = None) -> bool:
    opens = reapply_opens_at(verification)
    return opens is not None and (now or timezone.now()) >= opens


def is_editable(verification: CompanyVerification) -> bool:
    """Whether documents may change and a submission may be sent now.

    Before the first submission, when a reviewer asked for more, and — from
    thirty days after the decision — after a rejection (D-02). Never while a
    reviewer is reading, so they read what the company actually sent.
    """
    if verification.state in (VerificationState.NOT_SUBMITTED, VerificationState.MORE_INFO):
        return True
    return can_reapply(verification)


_SLUG_MAX_ATTEMPTS = 50


@transaction.atomic
def create_company(*, actor: User, name: str, website: str = "", **fields: object) -> Company:
    """Register a company and make the creator its first owner.

    The membership is created in the same transaction as the company. A company
    without an owner is unreachable — nobody could invite anyone into it — so
    the two must commit or fail together.
    """
    name = _clean_profile_field("name", name)
    company = Company.objects.create(
        slug=_unique_slug(name),
        name=name,
        website=_clean_website(website),
        created_by=actor,
        **fields,
    )
    CompanyVerification.objects.create(company=company)

    CompanyMembership.objects.create(
        company=company,
        user=actor,
        role=Role.OWNER,
        joined_at=timezone.now(),
    )

    audit.record(verb="company.created", actor=actor, target=company)
    events.emit(
        "company.created",
        company,
        payload={"company_id": company.pk, "slug": company.slug, "created_by_id": actor.pk},
    )
    return company


@transaction.atomic
def update_company(*, actor: User, company: Company, row_version: int, **fields: str) -> Company:
    """Edit company details under optimistic locking.

    Two admins editing the same profile is ordinary, and last-write-wins on a
    company description is an unanswerable support ticket (DEVELOPER.md §2.7).
    """
    if company.row_version != row_version:
        raise StaleWrite()

    editable = {
        "name",
        "legal_name",
        "website",
        "tagline",
        "size_band",
        "industry_id",
        "location_id",
        "about",
        "founded",
    }
    unknown = set(fields) - editable
    if unknown:
        raise ValueError(f"Not editable here: {sorted(unknown)}")

    for key, value in fields.items():
        setattr(company, key, _clean_profile_field(key, value))
    company.row_version += 1
    company.save(update_fields=[*fields, "row_version", "updated_at"])

    audit.record(
        verb="company.updated", actor=actor, target=company, metadata={"fields": sorted(fields)}
    )
    return company


@transaction.atomic
def submit_verification(*, actor: User, company: Company) -> CompanyVerification:
    """Move verification into review. Postings stay live while it is pending.

    Domain proof is not required (decision D-07): it is extra evidence a
    reviewer sees beside the documents, and many small companies here use a
    free-mail address. The documents are required.

    After a rejection this starts a new attempt, from thirty days after the
    decision (D-02). The rejection itself stays on record in
    `CompanyVerificationDecision`; nothing here rewrites it.
    """
    verification = CompanyVerification.objects.select_for_update().get(company=company)
    # A suspended company cannot put itself in front of a reviewer.
    policies.require_company_active(company)

    if verification.state == VerificationState.REJECTED and not can_reapply(verification):
        opens = reapply_opens_at(verification)
        when = f" from {timezone.localtime(opens):%d %b %Y}" if opens else ""
        raise InvalidState(
            f"This company was not verified. You can apply again{when}, "
            f"{REAPPLY_AFTER_DAYS} days after the decision."
        )
    if not verification.documents.exists():
        raise InvalidState("Attach your registration documents before submitting.")

    if verification.state in (VerificationState.NOT_SUBMITTED, VerificationState.REJECTED):
        verification.attempt += 1
    elif verification.attempt == 0:
        verification.attempt = 1
    verification.submitted_by = actor

    _transition(
        actor=actor,
        company=company,
        verification=verification,
        target=VerificationState.PENDING,
        submitted=True,
    )
    return verification


#: How long a claim holds the item (SRS §19.5). Long enough to read documents
#: and check a registration number, short enough that an abandoned review
#: returns to the queue the same morning.
CLAIM_MINUTES = 45


#: What a company may attach (SRS §5.6 `CompanyVerificationDocument.kind`). The
#: two the verification screen has always asked for. Scans of paper
#: certificates are images as often as PDFs, so both are accepted.
DOCUMENT_KINDS = VERIFICATION_DOCUMENT_KINDS
DOCUMENT_TYPES = file_constants.DOCUMENT_TYPES | file_constants.IMAGE_TYPES
MAX_VERIFICATION_DOCUMENTS = 10

#: What a reviewer needs in order to decide: the company is who it says it is
#: (registration) and it is the trading entity it says it is (PAN or VAT).
#: Both kinds, in that order.
REQUIRED_DOCUMENT_KINDS: tuple[str, ...] = ("registration", "pan_vat")


def missing_document_kinds(verification: CompanyVerification) -> list[str]:
    """The required kinds this submission has no usable file for.

    "Usable" excludes a file that was quarantined or failed its scan: a
    reviewer cannot open it, so as evidence it is not there, and a company
    that uploaded an infected scan and saw it counted would be waiting on a
    review that could only ask for it again. A file still being scanned
    counts as present — it is on its way, and saying it is missing while the
    scanner runs is a sentence that contradicts itself a minute later.

    Advisory, deliberately. `submit_verification` does not refuse an
    incomplete submission: some companies genuinely hold one certificate,
    and the workflow already has the state for "not enough yet" — a reviewer
    asks for more information, with a reason. What this gives both sides is
    the same list, so neither is guessing.
    """
    from apps.files.constants import ScanStatus

    present = {
        document.kind
        for document in verification.documents.all()
        if document.attachment.deleted_at is None
        and document.attachment.scan_status in (ScanStatus.CLEAN, ScanStatus.PENDING)
    }
    return [kind for kind in REQUIRED_DOCUMENT_KINDS if kind not in present]


def submission_is_complete(verification: CompanyVerification) -> bool:
    """Whether every required document kind is attached and openable."""
    return not missing_document_kinds(verification)


def document_checklist(verification: CompanyVerification) -> list[dict[str, Any]]:
    """The required kinds with their labels and whether each one has arrived.

    One list built here rather than in two templates, because the employer
    screen and the staff queue must not be able to disagree about what is
    outstanding.
    """
    missing = set(missing_document_kinds(verification))
    return [
        {"kind": kind, "label": DOCUMENT_KINDS[kind], "present": kind not in missing}
        for kind in REQUIRED_DOCUMENT_KINDS
    ]


@transaction.atomic
def add_verification_document(
    *,
    actor: User,
    membership: CompanyMembership | None,
    company: Company,
    kind: str,
    uploaded_file: Any,
) -> CompanyVerificationDocument:
    """Attach one document to the company's verification.

    Stored with company visibility and the company as owner, so every active
    member can read it and nobody else can (SRS §15.2); staff reach it through
    the review screen, which logs the read.
    """
    policies.require_can_edit_company(membership)
    policies.require_company_active(company)
    verification = CompanyVerification.objects.select_for_update().get(company=company)

    if not is_editable(verification):
        raise InvalidState(
            "Documents can be added before you submit, when a reviewer asks for more "
            f"information, or from {REAPPLY_AFTER_DAYS} days after a rejection."
        )
    if kind not in DOCUMENT_KINDS:
        raise InvalidState("Choose which document this is.")
    if uploaded_file is None:
        raise InvalidState("Choose a file to upload.")
    if verification.documents.count() >= MAX_VERIFICATION_DOCUMENTS:
        raise InvalidState(
            f"A submission holds up to {MAX_VERIFICATION_DOCUMENTS} documents. Remove one first."
        )

    attachment = file_service.store(
        uploaded_file=uploaded_file,
        visibility=file_constants.Visibility.COMPANY,
        accepted_types=DOCUMENT_TYPES,
        uploaded_by=actor,
    )
    attachment.owner_company = company
    attachment.save(update_fields=["owner_company", "updated_at"])

    document = CompanyVerificationDocument.objects.create(
        verification=verification, kind=kind, attachment=attachment, uploaded_by=actor
    )
    audit.record(
        verb="company.verification_document_added",
        actor=actor,
        target=company,
        company=company,
        metadata={"kind": kind, "filename": attachment.filename},
    )
    return document


@transaction.atomic
def remove_verification_document(
    *, actor: User, membership: CompanyMembership | None, company: Company, document_id: Any
) -> None:
    """Take a document back out of a submission that is not under review."""
    policies.require_can_edit_company(membership)
    policies.require_company_active(company)
    verification = CompanyVerification.objects.select_for_update().get(company=company)
    if not is_editable(verification):
        raise InvalidState("Documents cannot change while a reviewer is looking at them.")

    try:
        document = verification.documents.select_related("attachment").get(pk=int(document_id))
    except (TypeError, ValueError, CompanyVerificationDocument.DoesNotExist):
        raise NotFound("That document does not exist, or you cannot see it.") from None

    attachment = document.attachment
    document.delete()
    # A document a reviewer already decided on is evidence for that decision
    # (D-02): it leaves the new submission, but the file itself is kept.
    decided_on = verification.decisions.filter(
        documents__contains=[{"attachment_id": attachment.pk}]
    ).exists()
    if not decided_on:
        file_service.soft_delete(attachment=attachment, actor=actor)
    audit.record(
        verb="company.verification_document_removed",
        actor=actor,
        target=company,
        company=company,
        metadata={"kind": document.kind, "filename": attachment.filename},
    )


#: Why a reviewer may not handle a company's verification (decision D-04).
CONFLICT_REASONS: dict[str, str] = {
    "member": "you are a member of this company",
    "invited": "you have a pending invitation to this company",
    "submitter": "you sent this submission yourself",
}


def reviewer_conflicts(actor: User, verifications: list[CompanyVerification]) -> dict[int, str]:
    """Company id -> the conflict code that bars `actor` from reviewing it (D-04).

    Batched for the queue screen: three queries however long the queue is.
    """
    company_ids = [verification.company_id for verification in verifications]
    if not company_ids:
        return {}
    member_of = set(
        CompanyMembership.objects.filter(
            user=actor,
            company_id__in=company_ids,
            deleted_at__isnull=True,
            status=CompanyMembership.Status.ACTIVE,
        ).values_list("company_id", flat=True)
    )
    # Every address on the account, verified or not: an invitation to an
    # address the reviewer holds is a conflict whether or not they confirmed it.
    addresses = {actor.email.lower()}
    addresses.update(
        address.lower() for address in actor.email_addresses.values_list("email", flat=True)
    )
    invited_to = {
        invite.company_id
        for invite in CompanyInvite.objects.filter(
            company_id__in=company_ids,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).only("company_id", "email")
        if invite.email.lower() in addresses
    }

    conflicts: dict[int, str] = {}
    for verification in verifications:
        if verification.company_id in member_of:
            conflicts[verification.company_id] = "member"
        elif verification.company_id in invited_to:
            conflicts[verification.company_id] = "invited"
        elif verification.submitted_by_id is not None and verification.submitted_by_id == actor.pk:
            conflicts[verification.company_id] = "submitter"
    return conflicts


def _refuse_conflict(*, actor: User, company: Company, operation: str) -> None:
    """Refuse a conflicted reviewer, and record that they tried (D-04).

    Checked and audited outside the operation's own transaction, so the audit
    row survives the refusal. There is no override at any staff level: another
    reviewer takes the item.
    """
    verification = CompanyVerification.objects.get(company=company)
    code = reviewer_conflicts(actor, [verification]).get(company.pk)
    if code is None:
        return
    audit.record(
        verb="company.verification_conflict_refused",
        actor=actor,
        target=company,
        company=company,
        metadata={"operation": operation, "conflict": code, "attempt": verification.attempt},
    )
    raise NotAllowed(
        f"You cannot review {company.name}: {CONFLICT_REASONS[code]}. "
        "Another reviewer needs to take it.",
        code="reviewer_conflict",
    )


def claim_verification(*, actor: User, company: Company) -> CompanyVerification:
    """Take an item off the queue so two reviewers do not duplicate the work.

    A lock with an expiry rather than an assignment: somebody who closes the
    tab must not remove an item from the queue permanently, and asking a
    reviewer to remember to release one is asking them to do the thing they
    are least likely to do (§19.5).

    A reviewer with a conflict of interest — a member, an invitee, or the
    person who submitted — is refused (decision D-04).
    """
    _refuse_conflict(actor=actor, company=company, operation="claim")
    return _claim(actor=actor, company=company)


@transaction.atomic
def _claim(*, actor: User, company: Company) -> CompanyVerification:
    verification = CompanyVerification.objects.select_for_update().get(company=company)

    if verification.state != VerificationState.PENDING:
        raise InvalidState("That submission is not waiting for review.")
    if verification.is_claimed and verification.claimed_by_id not in (None, actor.pk):
        raise Conflict("Somebody else is reviewing this one.")

    verification.claimed_by = actor
    verification.claimed_until = timezone.now() + dt.timedelta(minutes=CLAIM_MINUTES)
    verification.save(update_fields=["claimed_by", "claimed_until", "updated_at"])

    audit.record(verb="company.verification_claimed", actor=actor, target=company, company=company)
    return verification


@transaction.atomic
def release_verification(*, actor: User, company: Company) -> CompanyVerification:
    """Put an item back without deciding it.

    Only the reviewer holding a live claim can hand it back. Anyone else
    releasing it would be taking it from a colleague mid-review, which is the
    collision the claim exists to prevent (§19.5).
    """
    verification = CompanyVerification.objects.select_for_update().get(company=company)
    if verification.is_claimed and verification.claimed_by_id != actor.pk:
        raise Conflict("Somebody else is reviewing this one.")
    verification.claimed_by = None
    verification.claimed_until = None
    verification.save(update_fields=["claimed_by", "claimed_until", "updated_at"])

    audit.record(verb="company.verification_released", actor=actor, target=company, company=company)
    return verification


def expire_claims() -> int:
    """Return abandoned reviews to the queue.

    Belt and braces: `is_claimed` already treats an expired claim as unheld, so
    nothing depends on this running. It clears the columns so the queue screen
    does not show a reviewer's name against an item they walked away from an
    hour ago.
    """
    return CompanyVerification.objects.filter(
        claimed_until__lt=timezone.now(), claimed_by__isnull=False
    ).update(claimed_by=None, claimed_until=None)


def review_verification(
    *, actor: User, company: Company, decision: str, reason: str = ""
) -> CompanyVerification:
    """Staff decision. `decision` is a target state, validated by the machine.

    The decider must hold a live claim. Without that, the claim is advisory:
    a second reviewer could decide an item a colleague is halfway through
    reading, or decide one nobody has opened at all (§19.5). A conflict of
    interest is refused here as well as at the claim (D-04): a claim taken
    before the reviewer was invited or joined does not let them decide.
    """
    _refuse_conflict(actor=actor, company=company, operation="decide")
    return _decide(actor=actor, company=company, decision=decision, reason=reason)


@transaction.atomic
def _decide(*, actor: User, company: Company, decision: str, reason: str) -> CompanyVerification:
    verification = CompanyVerification.objects.select_for_update().get(company=company)

    if decision not in _DECISIONS:
        raise InvalidState("Choose verified, more information needed, or rejected.")
    if not verification.is_claimed:
        raise Conflict("Claim this submission before deciding it.")
    if verification.claimed_by_id != actor.pk:
        raise Conflict("Somebody else is reviewing this one.")

    if decision in {VerificationState.MORE_INFO, VerificationState.REJECTED} and not reason:
        raise InvalidState("Give a reason so the company knows what to do next.")

    submitted_at, submitted_by_id = verification.submitted_at, verification.submitted_by_id
    _transition(
        actor=actor,
        company=company,
        verification=verification,
        target=decision,
        reason=reason,
        reviewed=True,
    )
    # The permanent record of this decision (D-02). Written once, never changed.
    CompanyVerificationDecision.objects.create(
        verification=verification,
        attempt=verification.attempt,
        state=decision,
        reason=reason,
        reviewed_by=actor,
        decided_at=verification.reviewed_at or timezone.now(),
        submitted_at=submitted_at,
        submitted_by_id=submitted_by_id,
        documents=[
            {
                "kind": document.kind,
                "filename": document.attachment.filename,
                "attachment_id": document.attachment_id,
            }
            for document in verification.documents.select_related("attachment")
        ],
    )
    events.emit(
        "company.verification_reviewed",
        company,
        payload={
            "company_id": company.pk,
            "company_name": company.name,
            "to_state": decision,
            "outcome": _OUTCOME_WORDS[decision],
            "reason": reason,
            "attempt": verification.attempt,
            "target_url": reverse("employer:verification", args=[company.slug]),
        },
    )
    return verification


#: The three outcomes a reviewer may choose, and how each reads to the company.
_DECISIONS = (VerificationState.VERIFIED, VerificationState.MORE_INFO, VerificationState.REJECTED)
_OUTCOME_WORDS: dict[str, str] = {
    VerificationState.VERIFIED: "is verified",
    VerificationState.MORE_INFO: "needs more information",
    VerificationState.REJECTED: "was not verified",
}


#: Paragraphs on a company page. More than this is a brochure, and nobody
#: reads a brochure while deciding whether to apply.
MAX_ABOUT_PARAGRAPHS = 8
MAX_ABOUT_CHARS = 2000


def _clean_profile_field(key: str, value: Any) -> Any:
    """Normalise the fields a form can send in more than one shape."""
    if key == "website":
        return _clean_website(value)
    if key == "about":
        lines = value.split("\n\n") if isinstance(value, str) else list(value or [])
        paragraphs = [str(item).strip()[:MAX_ABOUT_CHARS] for item in lines if str(item).strip()]
        if len(paragraphs) > MAX_ABOUT_PARAGRAPHS:
            raise InvalidState(f"A company page holds up to {MAX_ABOUT_PARAGRAPHS} paragraphs.")
        return paragraphs
    if key in {"industry_id", "location_id"}:
        # An empty select posts "", which means "not stated" rather than zero.
        try:
            chosen = int(value) or None
        except (TypeError, ValueError):
            return None
        if chosen is None:
            return None
        from apps.taxonomy.models import Industry, Location

        model = Industry if key == "industry_id" else Location
        if not model.objects.filter(pk=chosen).exists():
            # A stale or hand-edited select reached the foreign key and failed
            # the save with a 500.
            raise InvalidState("Choose one of the listed options.")
        return chosen

    field = key.removesuffix("_id")
    text_limit = getattr(Company._meta.get_field(field), "max_length", None)
    if isinstance(value, str) or value is None:
        value = str(value or "").strip()
        if key == "name" and not value:
            raise InvalidState("A company needs a name.")
        if text_limit and len(value) > text_limit:
            # Unchecked, a long value reached PostgreSQL as a DataError and the
            # person saw a server error instead of a sentence.
            label = field.replace("_", " ")
            raise InvalidState(f"The {label} can be up to {text_limit} characters.")
    return value


_URL = URLValidator(schemes=["http", "https"])


def _clean_website(value: Any) -> str:
    """A website is blank or a real http(s) address.

    The model's URLField is not validated on `save()`, so "not a url" used to
    be stored and shown to reviewers and on the company page as if it were one.
    A bare domain gets `https://` in front, which is what people mean.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    try:
        _URL(text)
    except ValidationError:
        raise InvalidState(
            "Enter the company website as an address, like https://example.com."
        ) from None
    return text[:200]


def _transition(
    *,
    actor: User,
    company: Company,
    verification: CompanyVerification,
    target: str,
    reason: str = "",
    submitted: bool = False,
    reviewed: bool = False,
) -> None:
    previous = verification.state
    VERIFICATION.require(verification, target)

    verification.state = target
    verification.reason = reason
    if submitted:
        verification.submitted_at = timezone.now()
        verification.claimed_by = None
        verification.claimed_until = None
    if reviewed:
        verification.reviewed_by = actor
        verification.reviewed_at = timezone.now()
        # A decided item is off the queue; a claim left on it would show a
        # reviewer's name against work that is finished.
        verification.claimed_by = None
        verification.claimed_until = None
    verification.save()

    company.verification_state = target
    company.verified_at = timezone.now() if target == VerificationState.VERIFIED else None
    company.save(update_fields=["verification_state", "verified_at", "updated_at"])

    audit.record(
        verb="company.verification_state_changed",
        actor=actor,
        target=company,
        # The attempt makes a new submission after a rejection read as a new
        # attempt in the history, not a reopening of the old one (D-02).
        metadata={
            "from": previous,
            "to": target,
            "reason": reason,
            "attempt": verification.attempt,
        },
    )
    events.emit(
        "company.verification_state_changed",
        company,
        payload={
            "company_id": company.pk,
            "from_state": previous,
            "to_state": target,
            "attempt": verification.attempt,
        },
    )
    if target == VerificationState.VERIFIED:
        events.emit(
            "company.verified",
            company,
            payload={
                "company_id": company.pk,
                "slug": company.slug,
                "company_name": company.name,
                "target_url": reverse("jobs:company", args=[company.slug]),
            },
        )


@transaction.atomic
def suspend_company(*, actor: User, company: Company, reason: str) -> Company:
    """Suspension cascades (SRS §2.6).

    Memberships are left intact rather than revoked: suspension is reversible,
    and destroying the team on the way in would make reinstatement a rebuild.
    Access is refused by `CompanyMembership.is_active`, which consults the
    company — so one column change locks every surface at once.
    """
    reason = (reason or "").strip()
    if not reason:
        raise InvalidState("Record why this company is being suspended.")
    if company.state == CompanyState.SUSPENDED:
        raise InvalidState("That company is already suspended.")

    company.state = CompanyState.SUSPENDED
    company.save(update_fields=["state", "updated_at"])

    # "Cancels scheduled interviews with notice to candidates" (§2.6). Left
    # alone, a candidate turned up to a call with a company nobody could reach.
    cancelled = _cancel_upcoming_interviews(company=company, actor=actor)

    audit.record(
        verb="company.suspended",
        actor=actor,
        target=company,
        metadata={"reason": reason, "interviews_cancelled": cancelled},
    )
    events.emit(
        "company.suspended",
        company,
        payload={
            "company_id": company.pk,
            "company_name": company.name,
            "reason": reason,
        },
    )
    return company


@transaction.atomic
def reinstate_company(*, actor: User, company: Company, reason: str) -> Company:
    """Undo a suspension.

    Postings become visible again because they were only ever hidden by the
    company's state. Interviews cancelled on the way in stay cancelled: the
    candidates were told, and quietly re-booking them would contradict that.
    """
    reason = (reason or "").strip()
    if not reason:
        raise InvalidState("Record why this company is being reinstated.")
    if company.state != CompanyState.SUSPENDED:
        raise InvalidState("That company is not suspended.")

    company.state = CompanyState.ACTIVE
    company.save(update_fields=["state", "updated_at"])
    audit.record(
        verb="company.reinstated", actor=actor, target=company, metadata={"reason": reason}
    )
    events.emit(
        "company.reinstated",
        company,
        payload={
            "company_id": company.pk,
            "company_name": company.name,
            "reason": reason,
            "target_url": reverse("employer:overview", args=[company.slug]),
        },
    )
    return company


def _cancel_upcoming_interviews(*, company: Company, actor: User) -> int:
    from apps.interviews.models import InterviewSession, SessionState
    from apps.interviews.services import scheduling

    upcoming = InterviewSession.objects.filter(
        application__job__company=company,
        state__in=(SessionState.SCHEDULED, SessionState.IN_PROGRESS),
    ).select_related("application__job__company", "candidate")
    count = 0
    for session in upcoming:
        scheduling.cancel(
            session=session,
            actor=actor,
            reason=f"{company.name} is not able to hold interviews at the moment.",
        )
        count += 1
    return count


def _unique_slug(name: str) -> str:
    """A slug that is stable for the life of the company (ADR-A18).

    It appears in every employer URL, so it is generated once and never derived
    from the name again — renaming a company must not break its team's links.
    """
    base = slugify(name)[:60] or "company"
    if base not in RESERVED_SLUGS and not Company.objects.filter(slug=base).exists():
        return base

    for suffix in range(2, _SLUG_MAX_ATTEMPTS):
        candidate = f"{base}-{suffix}"
        if candidate not in RESERVED_SLUGS and not Company.objects.filter(slug=candidate).exists():
            return candidate

    raise Conflict("Could not allocate a unique address for that company name.")
