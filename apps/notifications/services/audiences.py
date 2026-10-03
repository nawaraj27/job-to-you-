"""
Who receives what (SRS §13.3).

An audience is a **query**, never a list. That is the load-bearing rule of
ADR-A25: the outbox worker plans chunks from `MIN(pk)`/`MAX(pk)` and each chunk
re-runs the query over its own key range, so fifty thousand recipients never
exist in one process's memory and a retried chunk resolves the same rows.

Each audience is registered by key. A `FanoutJob` stores the key rather than a
queryset because the chunk is executed in a different process from the one that
planned it, minutes or hours later.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from django.db.models import Q, QuerySet

if TYPE_CHECKING:  # pragma: no cover
    from ..models import OutboxEvent

#: audience key -> function(event) -> QuerySet[User], ordered by pk.
Resolver = Callable[["OutboxEvent"], "QuerySet"]

_AUDIENCES: dict[str, Resolver] = {}


class UnknownAudience(LookupError):
    pass


def register(key: str) -> Callable[[Resolver], Resolver]:
    """Declare an audience. Re-registering the same key is an error.

    Silently replacing one would let two modules disagree about who receives an
    event, and the loser would be whichever imported first — a difference
    nobody can see in a diff.
    """

    def decorator(fn: Resolver) -> Resolver:
        if key in _AUDIENCES and _AUDIENCES[key] is not fn:
            raise ValueError(f"Audience {key!r} is already registered.")
        _AUDIENCES[key] = fn
        return fn

    return decorator


def resolve(key: str, event: OutboxEvent) -> QuerySet:
    """The recipient queryset for one event, ordered by primary key."""
    resolver = _AUDIENCES.get(key)
    if resolver is None:
        raise UnknownAudience(f"No audience registered as {key!r}.")
    return resolver(event).order_by("pk")


def registry() -> dict[str, Resolver]:
    return dict(_AUDIENCES)


# --- the audiences --------------------------------------------------------


@register("event_actor")
def _event_actor(event: OutboxEvent) -> QuerySet:
    """Nobody. The default for an event with no human recipient.

    Registered rather than special-cased so that "this event notifies nobody"
    is a statement in the catalogue rather than a missing entry.
    """
    from apps.accounts.models import User

    return User.objects.none()


@register("application_candidate")
def _application_candidate(event: OutboxEvent) -> QuerySet:
    """The person who applied."""
    from apps.accounts.models import User
    from apps.applications.models import Application

    return (
        User.objects.filter(
            candidate_profile__applications__public_id=event.payload.get("application_public_id"),
            is_active=True,
        ).distinct()
        if Application.objects.exists()
        else User.objects.none()
    )


@register("application_company_team")
def _application_company_team(event: OutboxEvent) -> QuerySet:
    """The hiring team that should react to it: the people who can open it.

    Decision D-13 (SRS §2.3): a notification names an applicant and links to
    the application, so it goes only to members whose read reaches that
    application — Owner and Company Admin, and the recruiters and hiring
    managers assigned to the job. It used to reach every member, including
    recruiters on other jobs and roles whose link answered 403.
    """
    from apps.accounts.models import User
    from apps.applications import policies as application_policies
    from apps.applications.models import Application

    application = (
        Application.objects.select_related("job", "job__company")
        .filter(public_id=event.payload.get("application_public_id"))
        .first()
    )
    if application is None:
        return User.objects.none()

    return User.objects.filter(
        pk__in=application_policies.reader_user_ids(application), is_active=True
    )


@register("payload_users")
def _payload_users(event: OutboxEvent) -> QuerySet:
    """The specific people the emitting service named in `recipient_ids`.

    For events about particular people rather than a group: the member who was
    removed, the inviter whose invitation was accepted, both sides of an
    ownership transfer. The service decides who; this only resolves the ids.
    """
    from apps.accounts.models import User

    ids = [value for value in (event.payload.get("recipient_ids") or []) if isinstance(value, int)]
    if not ids:
        return User.objects.none()
    return User.objects.filter(pk__in=ids, is_active=True)


@register("company_team")
def _company_team(event: OutboxEvent) -> QuerySet:
    """Everyone on a company's team."""
    from apps.accounts.models import User

    company_id = event.payload.get("company_id")
    if not company_id:
        return User.objects.none()

    return User.objects.filter(
        company_memberships__company_id=company_id,
        company_memberships__status="active",
        company_memberships__deleted_at__isnull=True,
        is_active=True,
    ).distinct()


@register("company_editors")
def _company_editors(event: OutboxEvent) -> QuerySet:
    """The members who can act on the company's own record.

    Owners, admins, and anyone granted `company.edit`. Not the whole team: a
    verification request is work for somebody who can upload a document, and
    telling an interviewer about it is noise they cannot do anything with.
    """
    from django.db.models import Q

    from apps.accounts.models import User
    from apps.companies.models import CompanyMembership
    from apps.companies.permissions import ROLE_GRANTS, Grant

    company_id = event.payload.get("company_id")
    if not company_id:
        return User.objects.none()

    roles = [role for role, grants in ROLE_GRANTS.items() if Grant.COMPANY_EDIT in grants]
    members = (
        CompanyMembership.objects.active()
        .filter(company_id=company_id)
        .filter(Q(role__in=roles) | Q(permissions__contains=[Grant.COMPANY_EDIT.value]))
        .values("user_id")
    )
    return User.objects.filter(pk__in=members, is_active=True)


@register("conversation_other_side")
def _conversation_other_side(event: OutboxEvent) -> QuerySet:
    """Whoever did not send the message.

    Resolved from `sender_kind` rather than from the sender's memberships: a
    recruiter who has since left the company must not turn an old message into
    one the candidate sent.
    """
    from apps.accounts.models import User
    from apps.messaging.models import Conversation, SenderKind

    conversation = (
        Conversation.objects.select_related("candidate")
        .filter(public_id=event.payload.get("conversation_public_id"))
        .first()
    )
    if conversation is None:
        return User.objects.none()

    if event.payload.get("sender_kind") == SenderKind.COMPANY:
        return User.objects.filter(pk=conversation.candidate.user_id, is_active=True)

    # The company side: members who can open the inbox, and — for a thread about
    # an application — only those whose read reaches it (D-13).
    from apps.companies import policies as company_policies
    from apps.companies.models import CompanyMembership

    members = CompanyMembership.objects.active().filter(company_id=conversation.company_id)
    ids = [
        member.user_id
        for member in members.select_related("company")
        if company_policies.can_open_section(member, "messages")
    ]
    if conversation.application is not None:
        from apps.applications import policies as application_policies

        readers = set(application_policies.reader_user_ids(conversation.application))
        ids = [user_id for user_id in ids if user_id in readers]
    return User.objects.filter(pk__in=ids, is_active=True)


@register("saved_search_owner")
def _saved_search_owner(event: OutboxEvent) -> QuerySet:
    """The candidate whose alert matched."""
    from apps.accounts.models import User

    return User.objects.filter(
        candidate_profile__public_id=event.payload.get("profile_public_id"), is_active=True
    )


@register("saved_job_holders")
def _saved_job_holders(event: OutboxEvent) -> QuerySet:
    """Everyone who bookmarked a posting that is about to close or has closed.

    The one audience that is genuinely large, and the reason chunking exists.
    """
    from apps.accounts.models import User

    return User.objects.filter(
        candidate_profile__saved_jobs__job__public_id=event.payload.get("job_public_id"),
        is_active=True,
    ).distinct()


@register("company_followers")
def _company_followers(event: OutboxEvent) -> QuerySet:
    """Everyone following the company the event is about (FOL-01).

    Excludes anybody whose candidate profile blocked that company: a block
    means the two are invisible to each other, and it may have been made
    after the follow. Preferences and mutes are applied downstream by the
    channel layer like every other audience, so they are not repeated here.
    """
    from apps.accounts.models import User

    company_id = event.payload.get("company_id")
    # Only follows that existed when the event happened. The audience is
    # resolved when the outbox drains, not when the job was published, so
    # without this anybody who followed during a worker backlog was told about
    # postings from before they followed — the demo candidate got four notices
    # for one new job.
    return (
        User.objects.filter(
            company_follows__company_id=company_id,
            company_follows__created_at__lte=event.created_at,
            is_active=True,
        )
        .exclude(candidate_profile__blocked_companies__company_id=company_id)
        .distinct()
    )


@register("session_candidate")
def _session_candidate(event: OutboxEvent) -> QuerySet:
    """The candidate whose interview it is (P16).

    Resolved from the session rather than from a user id in the payload, so an
    event about a practice session — which has no employer and no application —
    still reaches the right person, and so a payload cannot name somebody the
    session does not belong to.
    """
    from apps.accounts.models import User

    return User.objects.filter(
        candidate_profile__interview_sessions__public_id=event.payload.get("session_public_id"),
        is_active=True,
    ).distinct()


@register("account_owner")
def _account_owner(event: OutboxEvent) -> QuerySet:
    """The one account the event is about (P19).

    Used by events that concern somebody's account rather than their work — an
    erasure request, for instance, where there is no application, company or
    conversation to resolve through. Filtered on `is_active`, so an event about
    an account that has already been closed reaches nobody.
    """
    from apps.accounts.models import User

    user_id = event.payload.get("user_id")
    if not user_id:
        return User.objects.none()
    return User.objects.filter(pk=user_id, is_active=True)


@register("subscription_owner")
def _subscription_owner(event: OutboxEvent) -> QuerySet:
    """Whoever is billed: one candidate, or a company's whole team (P18).

    Resolved from the subscription rather than from a user id in the payload,
    for the same reason as every other audience here — a payload cannot then
    name somebody the subscription does not belong to. A company's plan news
    reaches the team because a company is not a person, and the one member who
    happened to click Subscribe is not the only one it affects.
    """
    from apps.accounts.models import User

    public_id = event.payload.get("subscription_public_id")
    if not public_id:
        return User.objects.none()

    return User.objects.filter(
        Q(candidate_profile__subscriptions__public_id=public_id)
        | Q(
            company_memberships__company__subscriptions__public_id=public_id,
            company_memberships__status="active",
            company_memberships__deleted_at__isnull=True,
        ),
        is_active=True,
    ).distinct()


@register("interview_participants")
def _interview_participants(event: OutboxEvent) -> QuerySet:
    """Everybody expected in the room: the candidate and the interviewers.

    One audience rather than two because scheduling news is the same news for
    both sides — a moved interview is moved for everyone — and splitting it
    would mean two events carrying one fact, which is how the two drift.
    """
    from apps.accounts.models import User
    from apps.interviews.models import InterviewParticipant

    public_id = event.payload.get("session_public_id")
    # `InterviewParticipant.user` has `related_name="+"`, so there is no reverse
    # lookup from User. The query that used one raised on every event, and no
    # interview notification — booked, moved, cancelled or reminder — was ever
    # delivered to anybody. The panel is read from the participant table instead.
    panel = InterviewParticipant.objects.filter(session__public_id=public_id).values("user_id")
    return User.objects.filter(
        Q(candidate_profile__interview_sessions__public_id=public_id) | Q(pk__in=panel),
        is_active=True,
    ).distinct()
