"""
Account erasure (SRS §19.4, §15.5, FR-1.8).

Request → fourteen-day cancellable grace → an erasure job that walks every
store explicitly.

Three decisions carry the whole design.

**`erasure_started_at` is set before anything is deleted, and every AI task
checks it on entry.** Without that gate, a task already in flight writes resume
quotes back into `ResumeAnalysis` *after* the erasure finishes, silently
recreating exactly the data the person asked to have removed. It is the one
ordering mistake in this file that would be invisible in testing and fatal in
production.

**Each store is listed by hand.** Not a loop over related managers, not a
cascade: a table added next year must be a decision about erasure rather than
an accident in either direction. The table in §19.4 and `_STORES` below say the
same thing, and the test suite asserts the second matches the first.

**Applications are anonymised, never deleted.** The employer keeps that an
application existed and how it ended — which they may be legally obliged to
keep — while holding none of the candidate's personal data. Same for
`ResumeVersion`: redacted to a tombstone (ADR-A38) so the `sha256` still proves
*that* a document was submitted, with nothing left of what it said.

Two limits are stated to the person rather than implied: a CV already delivered
to an employer cannot be recalled, and text already sent to an AI provider
cannot be unsent — our copy goes, theirs is subject to their retention.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState
from apps.notifications import events

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile

logger = logging.getLogger(__name__)

#: How long somebody has to change their mind (§19.4). Long enough that a
#: request made in anger can be withdrawn, short enough that the promise means
#: something.
GRACE_DAYS = 14

#: Typed to confirm. The same guard as the profile-data button, because this is
#: the larger version of it.
CONFIRMATION = "ERASE"

#: What the tombstoned account looks like afterwards. An address rather than a
#: null, so the unique constraint on email still holds and every foreign key
#: still resolves — which is what makes anonymisation possible at all (A22).
TOMBSTONE_DOMAIN = "erased.invalid"


@dataclass
class ErasureReport:
    """What the job actually did, per store. Written to the audit row.

    Counts rather than identifiers: this record is kept forever to prove the
    erasure happened, and a record of an erasure that listed what was erased
    would defeat its own purpose.
    """

    stores: dict[str, int] = field(default_factory=dict)

    def record(self, store: str, count: int) -> None:
        self.stores[store] = self.stores.get(store, 0) + count

    @property
    def total(self) -> int:
        return sum(self.stores.values())


# --- the request side ------------------------------------------------------


@transaction.atomic
def request_erasure(*, user: User, actor: User, confirmation: str) -> User:
    """Start the grace period. Nothing is removed yet."""
    if (confirmation or "").strip().upper() != CONFIRMATION:
        raise InvalidState(f"Type {CONFIRMATION} to confirm. Nothing has been removed.")
    if user.erasure_requested_at is not None:
        raise InvalidState("An erasure is already scheduled for this account.")
    if user.erased_at is not None:
        raise InvalidState("This account has already been erased.")

    user.erasure_requested_at = timezone.now()
    user.save(update_fields=["erasure_requested_at"])

    audit.record(
        verb="account.erasure_requested",
        actor=actor,
        target=user,
        metadata={"effective_on": erasure_due_on(user).isoformat()},
    )
    events.emit(
        "account.erasure_requested",
        user,
        payload={"user_id": user.pk, "effective_on": erasure_due_on(user).isoformat()},
    )
    return user


@transaction.atomic
def cancel_erasure(*, user: User, actor: User) -> User:
    """Change of mind, any time before the job runs.

    Refused once `erasure_started_at` is set: at that point rows are already
    gone, and telling somebody their cancellation worked would be a lie with
    their data behind it.
    """
    if user.erasure_requested_at is None:
        raise InvalidState("No erasure is scheduled for this account.")
    if user.erasure_started_at is not None:
        raise InvalidState("The erasure has already begun and cannot be stopped.")

    user.erasure_requested_at = None
    user.save(update_fields=["erasure_requested_at"])
    audit.record(verb="account.erasure_cancelled", actor=actor, target=user)
    return user


def erasure_due_on(user: User) -> dt.date:
    """When the job will run. What the screen shows."""
    started = user.erasure_requested_at or timezone.now()
    return (started + dt.timedelta(days=GRACE_DAYS)).date()


def due_now(*, at: dt.datetime | None = None) -> list[User]:
    """Accounts whose grace period has run out."""
    from apps.accounts.models import User

    moment = at or timezone.now()
    return list(
        User.objects.filter(
            erasure_requested_at__lte=moment - dt.timedelta(days=GRACE_DAYS),
            erasure_started_at__isnull=True,
            erased_at__isnull=True,
        )
    )


# --- the erasure itself ----------------------------------------------------


def erase(*, user: User) -> ErasureReport:
    """Walk every store. The only function that removes anything.

    Not one transaction end to end, deliberately: this deletes from a dozen
    tables and purges object storage, and a single transaction holding locks
    across all of it would block the platform for the duration. Each store is
    atomic on its own, and the gate below makes a partial run safe to resume —
    every step is idempotent, so running it twice finishes what the first
    attempt started.
    """
    _open_the_gate(user)

    report = ErasureReport()
    profile = getattr(user, "candidate_profile", None)

    if profile is not None:
        _erase_ai_runs(user, report)
        _erase_analyses(profile, report)
        _erase_resume_documents(profile, report)
        _erase_applications(profile, report)
        _erase_interviews(profile, report)
        _erase_messages(user, report)
        # Profile last of the two, then the vector: `_anonymise_profile` saves
        # the whole row from an instance loaded before this walk began, so
        # nulling the embedding first would have that save write it straight
        # back. Ordering rather than a refresh, because the ordering is the
        # thing a reader can check.
        _anonymise_profile(profile, report)
        _erase_matching(profile, report)

    _anonymise_analytics(user, report)
    _anonymise_user(user, report)

    audit.record(
        verb="account.erased",
        actor=None,
        target=user,
        metadata={"stores": report.stores, "rows": report.total},
    )
    with transaction.atomic():
        events.emit(
            "candidate.erasure_completed",
            user,
            payload={"user_id": user.pk, "stores": sorted(report.stores)},
        )

    logger.info("erased account", extra={"user_id": user.pk, "rows": report.total})
    return report


@transaction.atomic
def _open_the_gate(user: User) -> None:
    """Set `erasure_started_at` first, and set the profile's copy with it.

    §19.4's load-bearing sentence. Every AI and analysis task checks this on
    entry and aborts, so a task already queued cannot write resume text back
    into a table after the erasure has passed it.
    """
    if user.erased_at is not None:
        raise InvalidState("This account has already been erased.")

    now = timezone.now()
    if user.erasure_started_at is None:
        user.erasure_started_at = now
        user.save(update_fields=["erasure_started_at"])

    profile = getattr(user, "candidate_profile", None)
    if profile is not None and profile.erasure_started_at is None:
        profile.erasure_started_at = now
        profile.save(update_fields=["erasure_started_at"])


def _erase_ai_runs(user: User, report: ErasureReport) -> None:
    """The store §19.4 calls the most commonly missed.

    Resume text was sent to a provider, and a copy of what was sent and what
    came back is held here. The rows survive — cost accounting and the eval
    harness read them — with their payloads emptied.
    """
    from apps.ai.models import AIRun

    updated = AIRun.objects.filter(user=user).update(
        input_snapshot={}, output={}, raw_output="", error=""
    )
    report.record("ai_runs", updated)


def _erase_analyses(profile: CandidateProfile, report: ErasureReport) -> None:
    """Hard delete: findings quote resume lines verbatim (§19.4)."""
    from apps.resumes.models import ResumeAnalysis

    deleted, _ = ResumeAnalysis.objects.filter(version__document__profile=profile).delete()
    report.record("analyses", deleted)


def _erase_resume_documents(profile: CandidateProfile, report: ErasureReport) -> None:
    """Versions redacted, everything derived deleted, bytes purged.

    The order matters: the version's own redaction removes its renders, so the
    attachments are collected first and soft-deleted after, and the purge job
    verifies their absence from the bucket rather than assuming it.
    """
    from apps.files import services as file_services
    from apps.files.models import Attachment
    from apps.resumes.models import ResumeDocument, ResumeParse, ResumeUpload, ResumeVersion
    from apps.resumes.services import versions as version_service

    documents = ResumeDocument.objects.filter(profile=profile)

    attachment_ids = set(
        ResumeUpload.objects.filter(profile=profile)
        .exclude(attachment__isnull=True)
        .values_list("attachment_id", flat=True)
    )

    redacted = 0
    for version in ResumeVersion.objects.filter(document__in=documents, redacted_at__isnull=True):
        version_service.redact(version=version, actor=None, reason="Account erasure")
        redacted += 1
    report.record("resume_versions_redacted", redacted)

    parses, _ = ResumeParse.objects.filter(upload__profile=profile).delete()
    report.record("resume_parses", parses)

    # Drafts and their snapshot ring: nothing references them, so they go.
    from apps.resumes.models import ResumeDraft

    drafts, _ = ResumeDraft.objects.filter(document__in=documents).delete()
    report.record("resume_drafts", drafts)

    uploads, _ = ResumeUpload.objects.filter(profile=profile).delete()
    report.record("resume_uploads", uploads)

    purged = 0
    for attachment in Attachment.objects.filter(pk__in=attachment_ids, deleted_at__isnull=True):
        file_services.soft_delete(attachment=attachment)
        purged += 1
    report.record("attachments", purged)


def _erase_applications(profile: CandidateProfile, report: ErasureReport) -> None:
    """Anonymised, not deleted (§19.4).

    The employer keeps that an application existed and how it ended. What goes
    is everything the candidate wrote: screening answers, the cover letter
    body, and the match snapshot's inputs.
    """
    from apps.applications.models import Application, CoverLetter

    applications = Application.objects.filter(candidate=profile)
    report.record("applications_anonymised", applications.update(answers={}))

    # `content` is the letter itself. `title` is the candidate's own words too,
    # and a list of titles is a list of the companies somebody applied to.
    letters = CoverLetter.objects.filter(profile=profile)
    report.record("cover_letters", letters.update(content="", title=""))


def _erase_interviews(profile: CandidateProfile, report: ErasureReport) -> None:
    """Transcripts are hard deleted; the session shell survives.

    An employer keeps that an interview happened and its outcome; the answers
    a candidate typed are theirs and go.
    """
    from apps.interviews.models import InterviewSession, InterviewTurn

    sessions = InterviewSession.objects.filter(candidate=profile)
    turns, _ = InterviewTurn.objects.filter(session__in=sessions).delete()
    report.record("interview_turns", turns)


def _erase_messages(user: User, report: ErasureReport) -> None:
    """The bodies go; the conversation shell survives (§19.4)."""
    from apps.messaging.models import Message

    report.record("messages", Message.objects.filter(sender=user).update(body=""))


def _erase_matching(profile: CandidateProfile, report: ErasureReport) -> None:
    """The profile vector is derived personal data and is partially invertible.

    Nulled rather than left: a 768-dimension embedding of somebody's CV is not
    anonymous just because it is numeric.
    """
    from apps.candidates.models import CandidateProfile

    report.record(
        "embeddings",
        CandidateProfile.objects.filter(pk=profile.pk, embedding__isnull=False).update(
            embedding=None, embedding_model="", embedded_at=None
        ),
    )


def _anonymise_profile(profile: CandidateProfile, report: ErasureReport) -> None:
    """The profile becomes a tombstone: present, resolvable, empty.

    Reuses the profile-data erasure that already exists rather than writing a
    second list of fields to clear — two lists would eventually disagree, and
    the one that forgot a field would be this one.
    """
    from apps.candidates.models import Visibility
    from apps.candidates.services import erasure as profile_erasure

    result = profile_erasure.erase_profile_data(
        profile=profile, actor=profile.user, confirmation=profile_erasure.CONFIRMATION
    )
    report.record("profile_rows", result.rows_deleted)

    profile.visibility = Visibility.NOBODY
    profile.is_searchable = False
    profile.save(update_fields=["visibility", "is_searchable"])


def _anonymise_analytics(user: User, report: ErasureReport) -> None:
    """`actor` nulled, aggregates kept (§19.4).

    The events keep their shape and lose their owner, which is what makes the
    rollups survive an erasure without a recount.
    """
    from apps.analytics.models import AnalyticsEvent

    report.record(
        "analytics_events", AnalyticsEvent.objects.filter(actor=user).update(actor=None, anon_id="")
    )


@transaction.atomic
def _anonymise_user(user: User, report: ErasureReport) -> None:
    """The account itself. Last, because everything above needed it.

    The row survives with an unusable identity: applications, audit rows and
    attachment-access records all point at it, and deleting it would either
    cascade through an employer's records or leave the audit of this erasure
    pointing at nothing.
    """
    user.email = f"erased-{user.pk}@{TOMBSTONE_DOMAIN}"
    user.full_name = ""
    user.is_active = False
    user.erased_at = timezone.now()
    user.set_unusable_password()
    user.save(update_fields=["email", "full_name", "is_active", "erased_at", "password"])
    report.record("accounts", 1)


#: The stores §19.4 lists, in the order they are walked. Named here so the test
#: suite can assert the implementation and the specification still agree — a
#: store added to one and not the other is the failure this exists to catch.
STORES = (
    "ai_runs",
    "analyses",
    "resume_versions_redacted",
    "resume_parses",
    "resume_drafts",
    "resume_uploads",
    "attachments",
    "applications_anonymised",
    "cover_letters",
    "interview_turns",
    "messages",
    "embeddings",
    "profile_rows",
    "analytics_events",
    "accounts",
)


def is_erasing(owner: Any) -> bool:
    """Whether work for this owner must abort (§19.4's gate).

    Called on entry by the AI runner and by analysis. Accepts a user or a
    profile, because callers hold one or the other and neither should have to
    convert.

    **Read from the database, not from the instance.** A task loads its subject
    at the start of a job and writes at the end; if the erasure began in
    between, the in-memory row still says `None` and the gate the whole design
    rests on would wave it through. One indexed lookup against a provider call
    is not a trade worth thinking about.
    """
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile

    if owner is None:
        return False
    if isinstance(owner, CandidateProfile):
        return CandidateProfile.objects.filter(
            pk=owner.pk, erasure_started_at__isnull=False
        ).exists()
    if isinstance(owner, User):
        return User.objects.filter(pk=owner.pk, erasure_started_at__isnull=False).exists()

    # An unsaved instance, or something else entirely: fall back to whatever it
    # is carrying rather than guessing at a query.
    return getattr(owner, "erasure_started_at", None) is not None
