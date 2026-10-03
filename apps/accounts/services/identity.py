"""
Registration, email verification, password reset, sessions and consent.

Two reuses of P0 infrastructure are deliberate rather than convenient:

  * Verification and reset links are single-use because they are burned in the
    `ConsumedToken` ledger (ADR-A38). Django's reset token is already
    invalidated by a password change, but "already used" and "no longer valid"
    are different guarantees, and only the ledger gives the first one.
  * Every write records an audit entry and, where the world needs telling,
    an outbox event — never an inline side effect.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import signing
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.audit import services as audit
from apps.common import tokens
from apps.common.errors import Conflict, InvalidState, NotAllowed
from apps.common.uuid7 import uuid7
from apps.notifications import events

from ..models import Consent, EmailAddress, User, UserSession

if TYPE_CHECKING:  # pragma: no cover
    pass

VERIFY_SALT = "accounts.verify-email"
VERIFY_MAX_AGE = 60 * 60 * 24 * 3  # three days

_reset_tokens = PasswordResetTokenGenerator()


def email_is_registered(email: str) -> bool:
    """Whether an address already belongs to an account, as a login or a
    confirmed-or-not address on one. For the sign-up view to choose which email
    to send — never for a response a signed-out visitor can see (D-12)."""
    email = (email or "").strip().lower()
    return (
        User.objects.filter(email__iexact=email).exists()
        or EmailAddress.objects.filter(email__iexact=email).exists()
    )


@transaction.atomic
def register(
    *,
    email: str,
    password: str,
    full_name: str = "",
    signup_intent: str = User.SignupIntent.CANDIDATE,
    ip: str | None = None,
) -> User:
    """Create an account with an unverified primary address.

    Registration and verification are separate facts: `user.registered` fires
    now, `account.verified` fires later, and onboarding subscribes to the first
    while anything privileged waits for the second.

    `signup_intent` is an onboarding path, never an authority (SRS §2.1): it
    decides which navigation and which landing page this person sees, and the
    answer to "may they act for a company" stays a membership lookup. It is a
    parameter rather than a default because the sign-up form asks the question,
    and a form that asks a question whose answer is discarded is worse than one
    that never asked.
    """
    email = email.strip().lower()
    if User.objects.filter(email__iexact=email).exists():
        raise Conflict("An account with that email already exists.")

    if signup_intent not in {User.SignupIntent.CANDIDATE, User.SignupIntent.EMPLOYER}:
        # Staff is not self-service. Anything unrecognised lands on the safe
        # path rather than raising: a bad value in a UI hint must not stop
        # somebody creating an account.
        signup_intent = User.SignupIntent.CANDIDATE

    user = User.objects.create_user(
        email=email, password=password, full_name=full_name, signup_intent=signup_intent
    )
    EmailAddress.objects.create(user=user, email=email, is_primary=True)
    _record_signup_consent(user=user, ip=ip)

    audit.record(verb="user.registered", actor=user, target=user, ip=ip)
    events.emit(
        "user.registered",
        user,
        payload={"user_id": user.pk, "email": email, "signup_intent": signup_intent},
    )
    return user


def _record_signup_consent(*, user: User, ip: str | None) -> None:
    """Terms and privacy, at the moment they were accepted (SRS §15.5, A33).

    Creating an account is the acceptance — the sign-up form says so above the
    button — and until P19 nothing wrote it down. `Consent` exists to answer
    "what was agreed, when, and under which text" from one place, and a table
    nothing writes to answers it with silence.

    The version is read from the published document, so the row points at text
    that can still be produced. A system with no document seeded records an
    empty version rather than refusing the registration: somebody signing up
    must not be blocked by an operator's missing seed.
    """
    from apps.core import legal
    from apps.core.models import LegalDocument

    for slug, purpose in (
        (LegalDocument.Slug.TERMS, Consent.Purpose.TERMS),
        (LegalDocument.Slug.PRIVACY, Consent.Purpose.PRIVACY),
    ):
        record_consent(
            user=user,
            purpose=purpose,
            granted=True,
            policy_version=legal.version_of(slug),
            ip=ip,
            evidence={"source": "registration"},
        )


def issue_verification_token(*, address: EmailAddress) -> str:
    """Mint a signed, single-use verification token."""
    return signing.dumps({"aid": address.pk, "jti": str(uuid7())}, salt=VERIFY_SALT, compress=True)


@transaction.atomic
def verify_email(*, token: str, ip: str | None = None) -> EmailAddress:
    """Confirm an address. Idempotent per token, not per address."""
    try:
        payload = signing.loads(token, salt=VERIFY_SALT, max_age=VERIFY_MAX_AGE)
    except signing.SignatureExpired:
        raise NotAllowed("That link has expired. Request a new one.") from None
    except signing.BadSignature:
        raise NotAllowed("That link is not valid.") from None

    if not tokens.consume(
        jti=payload["jti"],
        # Its own namespace. It was burned as a password reset, so the ledger
        # recorded every confirmed address as a reset nobody had asked for.
        purpose=tokens.Purpose.EMAIL_VERIFICATION,
        expires_at=timezone.now() + timedelta(seconds=VERIFY_MAX_AGE),
    ):
        raise NotAllowed("That link has already been used.")

    address = EmailAddress.objects.select_for_update().filter(pk=payload["aid"]).first()
    if address is None:
        raise NotAllowed("That link is not valid.")

    if address.verified_at is None:
        address.verified_at = timezone.now()
        address.save(update_fields=["verified_at", "updated_at"])

        audit.record(verb="account.verified", actor=address.user, target=address.user, ip=ip)
        events.emit(
            "account.verified",
            address.user,
            payload={"user_id": address.user_id, "email": address.email},
        )
    return address


def request_password_reset(*, email: str) -> tuple[User, str] | None:
    """Return the user and a reset token, or None when no account matches.

    None rather than an exception, and the caller always shows the same message:
    telling an attacker which addresses exist turns the reset form into an
    account-enumeration oracle.
    """
    user = User.objects.filter(email__iexact=email.strip(), is_active=True).first()
    if user is None:
        return None
    return user, _reset_tokens.make_token(user)


@transaction.atomic
def reset_password(*, user: User, token: str, new_password: str, ip: str | None = None) -> User:
    """Set a new password, burning the token so it cannot be replayed."""
    if not _reset_tokens.check_token(user, token):
        raise NotAllowed("That reset link is not valid or has expired.")

    if not tokens.consume(
        jti=f"reset:{user.pk}:{token}",
        purpose=tokens.Purpose.PASSWORD_RESET,
        expires_at=timezone.now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT),
    ):
        raise NotAllowed("That reset link has already been used.")

    user.set_password(new_password)
    user.save(update_fields=["password"])

    revoke_all_sessions(user=user, except_key=None)
    audit.record(verb="account.password_reset", actor=user, target=user, ip=ip)
    return user


# --- sessions -------------------------------------------------------------


def record_session(*, user: User, session_key: str, ip: str | None, user_agent: str) -> UserSession:
    session, _ = UserSession.objects.update_or_create(
        session_key=session_key,
        defaults={
            "user": user,
            "ip": ip,
            "user_agent": user_agent[:400],
            "last_active_at": timezone.now(),
            "revoked_at": None,
        },
    )
    return session


@transaction.atomic
def revoke_session(*, actor: User, session_key: str) -> None:
    """Revoke one session. Only the owner may."""
    session = UserSession.objects.filter(session_key=session_key, user=actor).first()
    if session is None:
        raise NotAllowed("That session does not exist.")

    _delete_session(session_key)
    session.revoked_at = timezone.now()
    session.save(update_fields=["revoked_at"])
    audit.record(verb="account.session_revoked", actor=actor, target=actor)


@transaction.atomic
def revoke_all_sessions(*, user: User, except_key: str | None = None) -> int:
    """Sign out everywhere. Called on password reset, and by the user."""
    sessions = UserSession.objects.filter(user=user, revoked_at__isnull=True)
    if except_key:
        sessions = sessions.exclude(session_key=except_key)

    keys = list(sessions.values_list("session_key", flat=True))
    for key in keys:
        _delete_session(key)

    sessions.update(revoked_at=timezone.now())
    if keys:
        audit.record(
            verb="account.sessions_revoked", actor=user, target=user, metadata={"count": len(keys)}
        )
    return len(keys)


def _delete_session(session_key: str) -> None:
    """Delete a session through the store the site actually uses.

    It used the database store directly. Sessions are `cached_db`, which serves
    the Redis copy before it looks at the database, so a revoked session — and
    every session a password reset was meant to end — carried on working from
    the cache until it expired.
    """
    from importlib import import_module

    store = import_module(settings.SESSION_ENGINE).SessionStore
    store(session_key=session_key).delete()


def active_sessions(user: User) -> QuerySet[UserSession]:
    """This person's signed-in devices, most recent first (FR-1.4).

    Only sessions that can still be used: not revoked, and still present and
    unexpired in the session store. A `UserSession` row is written at sign-in
    and nothing marks it when the session simply expires, so the list showed
    every browser somebody had ever used as signed in.
    """
    from django.contrib.sessions.models import Session

    live = Session.objects.filter(expire_date__gt=timezone.now()).values("session_key")
    return UserSession.objects.filter(
        user=user, revoked_at__isnull=True, session_key__in=live
    ).order_by("-created_at")


@transaction.atomic
def change_password(
    *, user: User, current_password: str, new_password: str, keep_session_key: str | None
) -> int:
    """Change a password knowing the current one, and sign out everywhere else.

    Returns how many other sessions were ended. The current one survives: the
    person changing their password is the one person who should stay signed in.
    """
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    if not user.check_password(current_password):
        raise NotAllowed("Your current password is not correct.")
    if current_password == new_password:
        raise InvalidState("Choose a password you are not already using.")
    try:
        validate_password(new_password, user=user)
    except ValidationError as exc:
        raise InvalidState(" ".join(exc.messages)) from None

    user.set_password(new_password)
    user.save(update_fields=["password"])
    ended = revoke_all_sessions(user=user, except_key=keep_session_key)
    audit.record(verb="account.password_changed", actor=user, target=user)
    return ended


# --- consent --------------------------------------------------------------


@transaction.atomic
def record_consent(
    *,
    user: User,
    purpose: str,
    granted: bool,
    policy_version: str,
    ip: str | None = None,
    evidence: dict | None = None,
) -> Consent:
    """Append a consent decision. Never updates an earlier one."""
    if purpose not in set(Consent.Purpose.values):
        raise InvalidState(f"Unknown consent purpose: {purpose!r}")

    record = Consent.objects.create(
        user=user,
        purpose=purpose,
        granted=granted,
        policy_version=policy_version,
        ip=ip,
        evidence=evidence or {},
    )
    audit.record(
        verb="consent.changed",
        actor=user,
        target=user,
        metadata={"purpose": purpose, "granted": granted, "policy_version": policy_version},
    )
    events.emit(
        "consent.changed",
        user,
        payload={"user_id": user.pk, "purpose": purpose, "granted": granted},
    )
    return record


def has_consent(*, user: User, purpose: str) -> bool:
    """Current state is the newest row for that purpose."""
    latest = (
        Consent.objects.filter(user=user, purpose=purpose).order_by("-granted_at", "-pk").first()
    )
    return bool(latest and latest.granted and latest.revoked_at is None)
