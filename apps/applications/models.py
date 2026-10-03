"""
Applications and the hiring pipeline (SRS §5.8, ADR-A23, ADR-A34).

The shape of this module is set by three decisions, each of which removes a
duplicated fact rather than adding a feature:

  * **`stage` is the only state (A23).** There is no `status` column and no
    `Rejection` table. A rejection is a move to a `terminal` stage plus a
    `closed_reason`; anything said to the candidate rides in the event payload.
    v1.0 held that one fact in four places, and they disagreed.
  * **`ApplicationEvent` is the single source of both timelines (FR-6.4).** The
    candidate's view and the employer's activity log are two filters over one
    append-only stream. Two streams drift, and the direction they drift in is
    always the one where the candidate is told less than what happened.
  * **Contact details are revealed by stage, not at application (A34).** The
    application carries no phone number; `contact_revealed_at` records when the
    employer earned it. Cheap here, near-impossible to retrofit once employers
    expect the data.

`job_snapshot` is what makes A23's removal of `JobRevision` safe: an applicant
sees the posting as it was when they applied, from the row that needs it.
"""

from __future__ import annotations

import re

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import PublicIdModel, TimeStampedModel

#: Bumped when `Job.as_snapshot()` changes shape, so a reader can tell an old
#: snapshot from a new one instead of guessing from which keys are present.
JOB_SNAPSHOT_SCHEMA = 1


class StageKind(models.TextChoices):
    """What a stage *means*, independent of what a company calls it.

    Every derived question — is this application live, was it rejected, may an
    offer be sent — is answered from `kind`, so a company renaming "Shortlisted"
    to "Worth a call" changes a label and nothing else.
    """

    OPEN = "open", _("In progress")
    INTERVIEW = "interview", _("Interviewing")
    OFFER = "offer", _("Offer")
    TERMINAL = "terminal", _("Closed")


class PipelineStageQuerySet(models.QuerySet):
    def for_company(self, company) -> PipelineStageQuerySet:
        """A company's own stages, falling back to the platform defaults.

        The fallback is the point: a company that never opened the pipeline
        settings still has a working pipeline.
        """
        own = self.filter(company=company)
        return own if own.exists() else self.filter(company__isnull=True)

    def defaults(self) -> PipelineStageQuerySet:
        return self.filter(company__isnull=True)


class PipelineStage(TimeStampedModel):
    company = models.ForeignKey(
        "companies.Company",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pipeline_stages",
        help_text="Null means the platform default set, shared by every company.",
    )
    key = models.SlugField(max_length=40)
    label = models.CharField(max_length=60)
    order = models.PositiveSmallIntegerField(default=0)
    kind = models.CharField(max_length=16, choices=StageKind.choices, default=StageKind.OPEN)

    is_visible_to_candidate = models.BooleanField(
        default=True,
        help_text="When false the candidate sees `candidate_label` instead of the real name.",
    )
    candidate_label = models.CharField(
        max_length=60,
        blank=True,
        help_text="What the candidate is told. Blank means they see `label` (FR-6.10).",
    )
    reveals_contact = models.BooleanField(
        default=False,
        help_text="Reaching this stage unlocks the candidate's contact details (ADR-A34).",
    )

    objects = PipelineStageQuerySet.as_manager()

    class Meta:
        ordering = ("order", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "key"],
                name="pipelinestage_key_per_company",
                condition=models.Q(company__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["key"],
                name="pipelinestage_key_default",
                condition=models.Q(company__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return self.label

    @property
    def public_label(self) -> str:
        """What a candidate may be told this stage is called (FR-6.10)."""
        if self.is_visible_to_candidate:
            return self.label
        return self.candidate_label or "In progress"

    @property
    def is_terminal(self) -> bool:
        return self.kind == StageKind.TERMINAL


class ApplicationSource(models.TextChoices):
    # Brand-free on purpose: a label that embeds SITE_NAME makes the migration
    # state depend on an environment variable (docs/BRANDING.md).
    PLATFORM = "platform", _("Applied directly")
    INVITED = "invited", _("Invited by the employer")
    IMPORTED = "imported", _("Imported")


class ApplicationQuerySet(models.QuerySet):
    def live(self) -> ApplicationQuerySet:
        """Still in play: not withdrawn, not closed."""
        return self.filter(withdrawn_at__isnull=True, closed_at__isnull=True)

    def for_company(self, company) -> ApplicationQuerySet:
        """The tenant boundary. Every employer read starts here (ADR-A18)."""
        return self.filter(job__company=company)

    def for_candidate(self, profile) -> ApplicationQuerySet:
        return self.filter(candidate=profile)

    def visible_to(self, membership) -> ApplicationQuerySet:
        """What one team member may read.

        The rule is `policies.read_scope` (SRS §2.3, decision D-13): Owner and
        Company Admin company-wide, Recruiters and Hiring Managers their
        assigned jobs, Interviewers their panels.
        """
        import datetime as dt

        from django.db.models import Q
        from django.utils import timezone

        from . import policies

        queryset = self.for_company(membership.company)
        scope = policies.read_scope(membership)
        if scope == policies.SCOPE_COMPANY:
            return queryset
        if scope == policies.SCOPE_ASSIGNED:
            return queryset.filter(job__assignments__membership=membership).distinct()

        # On a panel, from scheduling until thirty days after the interview.
        cutoff = timezone.now() - dt.timedelta(days=policies.INTERVIEWER_DAYS_AFTER)
        # Measured the way `interviews.policies` measures it: from the end, or
        # from the scheduled time when it never ended, or from creation.
        on_panel = Q(interview_sessions__participants__user_id=membership.user_id)
        recent = (
            Q(interview_sessions__ended_at__gte=cutoff)
            | Q(
                interview_sessions__ended_at__isnull=True,
                interview_sessions__scheduled_at__gte=cutoff,
            )
            | Q(
                interview_sessions__ended_at__isnull=True,
                interview_sessions__scheduled_at__isnull=True,
                interview_sessions__created_at__gte=cutoff,
            )
        )
        sessions = on_panel & recent
        return queryset.filter(sessions).distinct()


class Application(PublicIdModel, TimeStampedModel):
    job = models.ForeignKey("jobs.Job", on_delete=models.PROTECT, related_name="applications")
    candidate = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="applications"
    )

    # The frozen artefact (ADR-A8, ADR-A19). An immutable version plus the hash
    # of the exact file the employer will download: "the CV I sent" has to mean
    # one thing months later, when the candidate's live CV has moved on.
    resume_version = models.ForeignKey(
        "resumes.ResumeVersion", on_delete=models.PROTECT, related_name="applications"
    )
    submitted_render_sha256 = models.CharField(max_length=64, blank=True)
    cover_letter = models.ForeignKey(
        "applications.CoverLetter",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications",
    )

    stage = models.ForeignKey(PipelineStage, on_delete=models.PROTECT, related_name="applications")
    answers = models.JSONField(
        default=dict, blank=True, help_text="Screening answers, keyed by question id."
    )
    source = models.CharField(
        max_length=16, choices=ApplicationSource.choices, default=ApplicationSource.PLATFORM
    )

    job_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="The posting as it read when this application was made (A23).",
    )
    job_snapshot_schema_version = models.PositiveSmallIntegerField(default=JOB_SNAPSHOT_SCHEMA)
    match_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "The match score at the moment of applying. Empty until P13 exists — "
            "the column is here so the score is frozen with the application "
            "rather than recomputed later against a changed profile."
        ),
    )

    contact_revealed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(max_length=60, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    applied_at = models.DateTimeField(default=timezone.now)
    last_activity_at = models.DateTimeField(default=timezone.now)

    objects = ApplicationQuerySet.as_manager()

    class Meta:
        ordering = ("-applied_at", "-pk")
        constraints = [
            # One live application per role. Partial, so a withdrawn or closed
            # application does not block a later one (FR-6.1, FR-6.14).
            models.UniqueConstraint(
                fields=["job", "candidate"],
                condition=models.Q(withdrawn_at__isnull=True, closed_at__isnull=True),
                name="application_one_live_per_job",
            ),
        ]
        indexes = [
            models.Index(fields=["job", "stage", "-applied_at"], name="application_job_idx"),
            models.Index(fields=["candidate", "-applied_at"], name="application_candidate_idx"),
            models.Index(fields=["candidate", "stage"], name="application_cand_stage_idx"),
        ]

    def __str__(self) -> str:
        return f"application {self.public_id}"

    @property
    def is_live(self) -> bool:
        return self.withdrawn_at is None and self.closed_at is None

    @property
    def is_withdrawn(self) -> bool:
        return self.withdrawn_at is not None

    @property
    def contact_is_revealed(self) -> bool:
        return self.contact_revealed_at is not None

    @property
    def is_closed_stage(self) -> bool:
        """At a terminal stage (hired, not proceeding), whatever it is called."""
        return self.stage.is_terminal

    @property
    def candidate_status(self) -> str:
        """What the candidate is told, never an internal stage name (FR-6.10)."""
        if self.withdrawn_at is not None:
            return "Withdrawn"
        return self.stage.public_label


class ActorKind(models.TextChoices):
    CANDIDATE = "candidate", _("Candidate")
    EMPLOYER = "employer", _("Employer")
    SYSTEM = "system", _("System")
    AI = "ai", _("AI")


class ApplicationEvent(models.Model):
    """The single source of both timelines (FR-6.4).

    Append-only: no `updated_at`, no service that edits one. The employer's
    activity log is every row; the candidate's timeline is the rows with
    `visible_to_candidate`. Two separate streams would drift, and the drift is
    always in the direction of the candidate being told less than what happened.
    """

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="timeline")
    actor = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    actor_kind = models.CharField(max_length=16, choices=ActorKind.choices)
    type = models.CharField(max_length=60)

    from_stage = models.ForeignKey(
        PipelineStage, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    to_stage = models.ForeignKey(
        PipelineStage, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    payload = models.JSONField(default=dict, blank=True)
    visible_to_candidate = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=["application", "-created_at"], name="appevent_application_idx")
        ]

    def __str__(self) -> str:
        return f"{self.type} on {self.application_id}"


class ApplicationNote(TimeStampedModel):
    """Company-internal, permanently (FR-6.11).

    There is no candidate-visible variant and no flag that could make one:
    a note is where a recruiter writes what they actually think, and a product
    that might one day surface it is a product where nobody writes anything
    useful.
    """

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField()
    mentions = models.JSONField(
        default=list, blank=True, help_text="Membership ids mentioned in the body."
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [models.Index(fields=["application", "-created_at"], name="appnote_app_idx")]

    def __str__(self) -> str:
        return self.body[:60]


class Tag(TimeStampedModel):
    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="application_tags"
    )
    name = models.CharField(max_length=40)
    colour = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="tag_name_per_company")
        ]

    def __str__(self) -> str:
        return self.name


class ApplicationTag(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="tag_links")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="application_links")
    added_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["application", "tag"], name="applicationtag_once")
        ]

    def __str__(self) -> str:
        return f"{self.tag_id} on {self.application_id}"


class ApplicationRating(TimeStampedModel):
    """One score per rater, so a panel produces a spread rather than a fight
    over a single field."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="ratings")
    rater = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="+")
    score = models.PositiveSmallIntegerField()
    comment = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["application", "rater"], name="applicationrating_one_per_rater"
            ),
            models.CheckConstraint(
                condition=models.Q(score__gte=1, score__lte=5), name="applicationrating_1_to_5"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.score}/5 on {self.application_id}"


class LetterSource(models.TextChoices):
    """Where the words came from.

    Recorded because the candidate is the author of what they send, and a
    letter they wrote themselves and one a model drafted are different things
    to them even when the text is identical. `ai_edited` is the honest middle:
    a model drafted it and the candidate changed it, which is the case the
    product is actually built for.
    """

    WRITTEN = "written", _("Written by the candidate")
    AI_DRAFT = "ai_draft", _("Drafted by AI, unedited")
    AI_EDITED = "ai_edited", _("Drafted by AI, then edited")


class LetterTone(models.TextChoices):
    """How the draft should read.

    Three, not a slider. A slider implies a continuum the model does not
    actually have, and every extra option is a decision the candidate has to
    make before they get any help at all.
    """

    DIRECT = "direct", _("Direct and plain")
    WARM = "warm", _("Warm")
    FORMAL = "formal", _("Formal")


_GREETING = re.compile(r"^(dear|hello|hi|to)\b[^,.:]{0,60}[,.:]\s*", re.IGNORECASE)


class CoverLetter(PublicIdModel, TimeStampedModel):
    """A letter, optionally written for one job.

    `ai_run` is the link P8 could not create — `AIRun` did not exist until P11 —
    and P14 is the phase that populates it (ADR-A11: every AI output is an
    `AIRun`, and a letter a model drafted must be traceable to the call that
    drafted it).

    The letter is **not** versioned like a resume. A `ResumeVersion` is
    immutable because an employer's copy of a CV must not change (ADR-A8); a
    cover letter is attached to an application by foreign key, and the
    application holds the one that was sent. Editing a letter that has already
    gone would rewrite what an employer read, so `is_sent` blocks it.
    """

    profile = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="cover_letters"
    )
    job = models.ForeignKey(
        "jobs.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="cover_letters"
    )
    title = models.CharField(max_length=120, blank=True)
    content = models.TextField()

    source = models.CharField(
        max_length=16, choices=LetterSource.choices, default=LetterSource.WRITTEN
    )
    tone = models.CharField(max_length=16, choices=LetterTone.choices, blank=True)
    ai_run = models.ForeignKey(
        "ai.AIRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cover_letters",
        help_text="The call that drafted this, when one did (ADR-A11).",
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["profile", "-created_at"], name="coverletter_profile_idx")]

    def __str__(self) -> str:
        return self.title or f"cover letter {self.pk}"

    @property
    def is_sent(self) -> bool:
        """Whether an application already carries this letter.

        A sent letter is part of what an employer read. Editing it would change
        the record of an application after the fact, which is the same class of
        problem ADR-A8 solves for CVs — solved here by refusing the edit and
        offering a copy instead.
        """
        return self.applications.exists()

    @property
    def word_count(self) -> int:
        return len(self.content.split())

    @property
    def opening(self) -> str:
        """The first words after the greeting, for telling letters apart.

        Every drafted letter begins "Dear hiring team," so a snippet of the
        first characters made different letters read identically.
        """
        text = " ".join((self.content or "").split())
        return _GREETING.sub("", text, count=1)


class OfferState(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SENT = "sent", _("Sent")
    ACCEPTED = "accepted", _("Accepted")
    DECLINED = "declined", _("Declined")
    WITHDRAWN = "withdrawn", _("Withdrawn")
    EXPIRED = "expired", _("Expired")


class Offer(TimeStampedModel):
    """One *live* offer per application, many historical (SRS §5.8).

    A unique-per-application constraint would have forced destructive edits:
    offers get revised, and the previous terms are exactly what a dispute is
    about.
    """

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="offers")
    salary = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    period = models.CharField(max_length=8, blank=True)
    salary_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    start_date = models.DateField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    attachment = models.ForeignKey(
        "files.Attachment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    message = models.TextField(blank=True)

    state = models.CharField(max_length=16, choices=OfferState.choices, default=OfferState.DRAFT)
    sent_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["application"],
                condition=models.Q(state__in=["draft", "sent"]),
                name="offer_one_live_per_application",
            )
        ]

    def __str__(self) -> str:
        return f"offer on {self.application_id} ({self.state})"

    @property
    def is_live(self) -> bool:
        return self.state in {OfferState.DRAFT, OfferState.SENT}

    @property
    def is_expired(self) -> bool:
        return (
            self.state == OfferState.SENT
            and self.expires_at is not None
            and self.expires_at <= timezone.now()
        )


class TalentPool(TimeStampedModel):
    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="talent_pools"
    )
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="talentpool_name_per_company")
        ]

    def __str__(self) -> str:
        return self.name


class TalentPoolMember(TimeStampedModel):
    pool = models.ForeignKey(TalentPool, on_delete=models.CASCADE, related_name="members")
    candidate = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="pool_memberships"
    )
    added_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pool", "candidate"], name="talentpoolmember_once")
        ]

    def __str__(self) -> str:
        return f"{self.candidate_id} in {self.pool_id}"


class CompanyCandidateNote(TimeStampedModel):
    """A note on a candidate who has no application (SRS §5.8).

    Separate from `ApplicationNote` rather than a nullable FK on it: a note
    about a person and a note about one of their applications are different
    facts with different lifetimes, and the application one dies with its row.
    """

    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="candidate_notes"
    )
    candidate = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="company_notes"
    )
    author = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField()
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["company", "candidate"], name="companycandnote_pair_idx"),
        ]

    def __str__(self) -> str:
        return self.body[:60]


class GrantBasis(models.TextChoices):
    APPLICATION = "application", _("They applied to us")
    PUBLIC = "public", _("Their profile is public")
    POOL = "pool", _("We saved them to a pool")
    INVITED = "invited", _("We invited them")


class VisibilityGrantQuerySet(models.QuerySet):
    def live(self) -> VisibilityGrantQuerySet:
        return self.filter(revoked_at__isnull=True).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=timezone.now())
        )


class CandidateVisibilityGrant(TimeStampedModel):
    """Why this company may read this candidate at all (INV-4).

    Every employer-side read of a candidate is authorised by a row here. The
    alternative — inferring permission from "they have an application
    somewhere" — is a rule that lives in whichever queryset last remembered it,
    and the failure is silent: a company reading a person who never dealt with
    them.
    """

    candidate = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="visibility_grants"
    )
    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="candidate_grants"
    )
    basis = models.CharField(max_length=16, choices=GrantBasis.choices)
    application = models.ForeignKey(
        Application, null=True, blank=True, on_delete=models.CASCADE, related_name="grants"
    )
    granted_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = VisibilityGrantQuerySet.as_manager()

    class Meta:
        ordering = ("-granted_at",)
        indexes = [
            models.Index(
                fields=["company", "candidate"],
                condition=models.Q(revoked_at__isnull=True),
                name="visibilitygrant_live_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.company_id} may see {self.candidate_id} ({self.basis})"

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()
