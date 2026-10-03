"""
Offers (FR-6.8).

A real state machine here, unlike the pipeline: the stages of an offer are not
a company's to configure. Draft becomes sent, sent becomes one of four
outcomes, and nothing goes backwards — an accepted offer that could be edited
would make "what did we agree" unanswerable.

One *live* offer per application, many historical. Revising an offer withdraws
the standing one and writes a new row, because the previous terms are exactly
what a dispute is about.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState, NotAllowed
from apps.common.state_machine import StateMachine, Transition
from apps.companies.permissions import Grant
from apps.notifications import events
from apps.taxonomy.services import money

from ..models import ActorKind, Application, Offer, OfferState
from . import pipeline

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import CompanyMembership

#: The only legal moves. Sending needs a grant; the candidate's answer does not,
#: because it is theirs to give.
OFFER = StateMachine(
    "state",
    transitions=[
        Transition(OfferState.DRAFT, OfferState.SENT, permission=Grant.OFFER_SEND),
        Transition(OfferState.DRAFT, OfferState.WITHDRAWN),
        Transition(OfferState.SENT, OfferState.ACCEPTED),
        Transition(OfferState.SENT, OfferState.DECLINED),
        Transition(OfferState.SENT, OfferState.WITHDRAWN),
        Transition(OfferState.SENT, OfferState.EXPIRED),
    ],
)

#: Default validity. Long enough to think, short enough that a forgotten offer
#: does not sit open forever.
DEFAULT_VALID_DAYS = 14


@transaction.atomic
def draft_offer(
    *,
    application: Application,
    actor: User,
    membership: CompanyMembership,
    salary: Any = None,
    currency: str = "",
    period: str = "year",
    start_date: dt.date | None = None,
    expires_at: dt.datetime | None = None,
    message: str = "",
    document: Any = None,
) -> Offer:
    """Write an offer without sending it.

    `document` is an optional offer letter (FR-6.8 "an attached document"). It
    is stored with company visibility, so the hiring team can open it, and is
    shared with the candidate only when the offer is sent.
    """
    if not application.is_live:
        raise InvalidState("This application is closed.")
    if application.offers.filter(state__in=[OfferState.DRAFT, OfferState.SENT]).exists():
        raise InvalidState("There is already a live offer. Withdraw it before writing another.")
    if application.offers.filter(state=OfferState.ACCEPTED).exists():
        # "One live offer at a time" let a second one be drafted the moment the
        # first was accepted, and an accepted offer is exactly the record a
        # later disagreement is about.
        raise InvalidState(
            "The candidate accepted an offer for this application. Record the hire "
            "rather than writing another offer."
        )
    if start_date is not None and start_date < timezone.localdate():
        # Accepted and sent before: a candidate read "starting 8 Sep 2026"
        # five days after that date.
        raise InvalidState("The start date is in the past. Choose today or a later date.")
    if expires_at is not None and expires_at <= timezone.now():
        # The form never offered an expiry, so every offer got the 14-day
        # default. Now that it can be chosen, a date already gone is refused
        # here rather than only at send.
        raise InvalidState("The offer must stay open until a date after today.")

    attachment = None
    if document is not None:
        from apps.files.constants import DOCUMENT_TYPES, Visibility
        from apps.files.services import store

        attachment = store(
            uploaded_file=document,
            visibility=Visibility.COMPANY,
            accepted_types=DOCUMENT_TYPES,
            uploaded_by=actor,
        )
        attachment.owner_company = application.job.company
        attachment.save(update_fields=["owner_company", "updated_at"])

    amount = _clean_amount(salary)
    offer = Offer(
        application=application,
        salary=amount,
        currency=(currency or "").upper()[:3],
        period=period or "year",
        start_date=start_date,
        expires_at=expires_at or timezone.now() + dt.timedelta(days=DEFAULT_VALID_DAYS),
        message=(message or "").strip()[:4000],
        attachment=attachment,
        state=OfferState.DRAFT,
    )
    _normalise(offer)
    offer.save()

    audit.record(
        verb="offer.drafted",
        actor=actor,
        target=application,
        company=application.job.company,
    )
    return offer


def document_url(offer: Offer | None, user: User) -> str:
    """A one-read link to the offer letter, or "" when there is none to open.

    The link is only a token; the file view still checks that this user may
    read the letter (a company member, or the candidate once it was sent).
    """
    attachment = offer.attachment if offer is not None and offer.attachment_id else None
    if attachment is None or not attachment.is_servable:
        return ""
    from apps.files import signing

    return signing.access_url(attachment, actor_id=user.pk, purpose="offer_letter")


@transaction.atomic
def send(*, offer: Offer, actor: User, membership: CompanyMembership) -> Offer:
    """Put the offer in front of the candidate."""
    OFFER.require(offer, OfferState.SENT, actor_permissions=membership.grants)

    if offer.salary is None:
        raise InvalidState("An offer without a number is not an offer.")
    if offer.expires_at is not None and offer.expires_at <= timezone.now():
        raise InvalidState("That expiry date has already passed.")

    offer.state = OfferState.SENT
    offer.sent_by = actor
    offer.sent_at = timezone.now()
    offer.save(update_fields=["state", "sent_by", "sent_at", "updated_at"])

    application = offer.application
    if offer.attachment is not None:
        # The letter becomes the candidate's to read at the moment the offer is
        # theirs to answer — not while it is still a draft (ADR-A52 grant row).
        from apps.files.services import share_with

        share_with(
            attachment=offer.attachment, user=application.candidate.user, reason="offer_letter"
        )
    pipeline.record_event(
        application=application,
        actor=actor,
        actor_kind=ActorKind.EMPLOYER,
        type="offer.sent",
        payload={"offer_id": offer.pk, "salary": str(offer.salary), "currency": offer.currency},
        visible_to_candidate=True,
    )
    events.emit(
        "offer.sent",
        application,
        payload={
            "application_public_id": str(application.public_id),
            "company_id": application.job.company_id,
            "offer_id": offer.pk,
        },
    )
    audit.record(
        verb="offer.sent",
        actor=actor,
        target=application,
        company=application.job.company,
        metadata={"offer_id": offer.pk},
    )
    return offer


def respond(*, offer: Offer, actor: User, accept: bool, reason: str = "") -> Offer:
    """The candidate's answer. Theirs alone — no grant is consulted.

    The expiry check runs **before** the transaction opens. Expiring the offer
    and refusing the answer are the same moment, and one atomic block would
    roll the expiry back along with the refusal — leaving an offer that is
    permanently past its date and permanently still 'sent'.
    """
    if offer.application.candidate.user_id != actor.pk:
        raise NotAllowed("Only the candidate can answer an offer.")
    if offer.is_expired:
        expire(offer=offer)
        raise InvalidState("That offer expired.")

    return _record_response(offer=offer, actor=actor, accept=accept, reason=reason)


@transaction.atomic
def _record_response(*, offer: Offer, actor: User, accept: bool, reason: str) -> Offer:
    target = OfferState.ACCEPTED if accept else OfferState.DECLINED
    OFFER.require(offer, target)

    offer.state = target
    offer.decided_at = timezone.now()
    offer.decline_reason = "" if accept else (reason or "").strip()[:200]
    offer.save(update_fields=["state", "decided_at", "decline_reason", "updated_at"])

    application = offer.application
    pipeline.record_event(
        application=application,
        actor=actor,
        actor_kind=ActorKind.CANDIDATE,
        type="offer.decided",
        payload={"offer_id": offer.pk, "state": target, "reason": offer.decline_reason},
        visible_to_candidate=True,
    )
    events.emit(
        "offer.decided",
        application,
        payload={
            "application_public_id": str(application.public_id),
            "company_id": application.job.company_id,
            "offer_id": offer.pk,
            "state": target,
            # The words the team's notification is written from. Without them
            # it read "The candidate has responded to your offer": no role, no
            # candidate, and no answer.
            "job_title": application.job.title,
            "answer": "accepted" if accept else "declined",
        },
    )
    audit.record(
        verb="offer.decided",
        actor=actor,
        target=application,
        company=application.job.company,
        metadata={"state": target},
    )
    return offer


@transaction.atomic
def withdraw(
    *, offer: Offer, actor: User, membership: CompanyMembership, reason: str = ""
) -> Offer:
    """The employer takes it back. Only while it is still live."""
    OFFER.require(offer, OfferState.WITHDRAWN, actor_permissions=membership.grants)

    offer.state = OfferState.WITHDRAWN
    offer.decided_at = timezone.now()
    offer.decline_reason = (reason or "").strip()[:200]
    offer.save(update_fields=["state", "decided_at", "decline_reason", "updated_at"])

    pipeline.record_event(
        application=offer.application,
        actor=actor,
        actor_kind=ActorKind.EMPLOYER,
        type="offer.withdrawn",
        payload={"offer_id": offer.pk, "reason": offer.decline_reason},
        visible_to_candidate=True,
    )
    audit.record(
        verb="offer.withdrawn",
        actor=actor,
        target=offer.application,
        company=offer.application.job.company,
    )
    return offer


@transaction.atomic
def expire(*, offer: Offer) -> Offer:
    """Time ran out. Run by the scheduler, never by a person."""
    OFFER.require(offer, OfferState.EXPIRED)

    offer.state = OfferState.EXPIRED
    offer.decided_at = timezone.now()
    offer.save(update_fields=["state", "decided_at", "updated_at"])

    pipeline.record_event(
        application=offer.application,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        type="offer.expired",
        payload={"offer_id": offer.pk},
        visible_to_candidate=True,
    )
    return offer


def expire_due(*, limit: int = 500) -> int:
    """Expire every sent offer whose date has passed.

    Row by row, because each expiry writes a timeline entry the candidate
    reads; a bulk `UPDATE` would change six hundred states and tell nobody.
    """
    overdue = Offer.objects.filter(
        state=OfferState.SENT, expires_at__isnull=False, expires_at__lte=timezone.now()
    ).select_related("application", "application__job")[:limit]

    expired = 0
    for offer in overdue:
        expire(offer=offer)
        expired += 1
    return expired


# --- internals ------------------------------------------------------------


def _clean_amount(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise InvalidState("Type the salary as a number, without symbols.") from None
    if amount <= 0:
        raise InvalidState("An offer's salary has to be a positive number.")
    return amount.quantize(Decimal("0.01"))


def _normalise(offer: Offer) -> None:
    """Annualise into the base currency (ADR-A39), keeping what was typed.

    A failure here is not fatal: an offer with an un-comparable figure is still
    a valid offer, and refusing to record it because an FX rate was missing
    would be the wrong trade.
    """
    if offer.salary is None or not offer.currency:
        offer.salary_base = None
        return
    try:
        normalised = money.normalise_salary(
            amount=offer.salary, currency=offer.currency, period=offer.period
        )
    except InvalidState:
        offer.salary_base = None
        return
    offer.salary_base = normalised.amount if normalised is not None else None
