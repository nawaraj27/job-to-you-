"""
Interviews (SRS §5.10, §11, FR-10.1 to FR-10.11).

**One session model, two engines.** `InterviewSession` is uniform across kinds;
only the engine differs, and every kind converges on `InterviewEvaluation` and
`InterviewOutcome` — so the pipeline, notifications and analytics have one shape
to handle rather than one per interview type. Video is P19 and adds tables here
without changing that shape.

Three structural decisions carry this module:

  * **Double-booking is prevented by Postgres**, not by a check-then-insert
    race. `InterviewSlot` carries a GiST exclusion constraint over
    `(interviewer, tstzrange(starts_at, ends_at))`; two confirmations landing in
    the same millisecond cannot both win, whatever the application does.
  * **A scorecard is invisible until it is submitted.** Anchoring is the most
    studied failure in interview panels, and the fix is a `submitted_at` column
    the selector filters on rather than a convention nobody enforces.
  * **`integrity_meta` is never a scoring input.** Paste and focus events are
    employer-visible context. Typing behaviour is not job performance, and a
    rubric that scored it would be measuring the wrong thing confidently.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import Func, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import PublicIdModel, TimeStampedModel


class InterviewKind(models.TextChoices):
    """The two engines v1 ships. Video is P19 (§21)."""

    AI_TEXT = "ai_text", _("AI text interview")
    #: Spoken practice: the candidate answers aloud, the answer is transcribed
    #: on the server and the audio discarded. Same engine, same scoring; the
    #: delivery measurements are feedback only (`services.delivery`).
    AI_VOICE = "ai_voice", _("AI video practice interview")
    LIVE = "live", _("Live interview")

    @classmethod
    def self_paced(cls) -> frozenset[str]:
        """Kinds the candidate takes alone, one question at a time."""
        return frozenset({cls.AI_TEXT, cls.AI_VOICE})


class SessionState(models.TextChoices):
    SCHEDULED = "scheduled", _("Scheduled")
    IN_PROGRESS = "in_progress", _("In progress")
    COMPLETED = "completed", _("Completed")
    ABANDONED = "abandoned", _("Abandoned")
    CANCELLED = "cancelled", _("Cancelled")
    NO_SHOW = "no_show", _("Nobody came")

    @classmethod
    def terminal(cls) -> frozenset[str]:
        """States nothing moves out of.

        `abandoned` is here and `in_progress` is not: a candidate who closed the
        tab has ended the session, and reopening it hours later would produce an
        interview whose time-boxes meant nothing.
        """
        return frozenset({cls.COMPLETED, cls.ABANDONED, cls.CANCELLED, cls.NO_SHOW})


class SlotState(models.TextChoices):
    OPEN = "open", _("Open")
    HELD = "held", _("Held")
    BOOKED = "booked", _("Booked")
    CANCELLED = "cancelled", _("Cancelled")


class QuestionOrigin(models.TextChoices):
    POOL = "pool", _("From the question pool")
    AI_FOLLOWUP = "ai_followup", _("A follow-up the model asked")


class ParticipantRole(models.TextChoices):
    INTERVIEWER = "interviewer", _("Interviewer")
    OBSERVER = "observer", _("Observer")


class Recommendation(models.TextChoices):
    """What an interview concluded. Deliberately four, not a score.

    A number invites averaging across interviewers, which is how a panel's
    disagreement disappears into a mean nobody argued for. These are positions
    somebody has to take.
    """

    STRONG_YES = "strong_yes", _("Strong yes")
    YES = "yes", _("Yes")
    NO = "no", _("No")
    STRONG_NO = "strong_no", _("Strong no")


class InterviewTemplate(TimeStampedModel):
    """What an interview asks, and how it is scored (SRS §5.10).

    `question_pools` is JSONB rather than a table, deliberately: the pool is
    never queried across templates, so it is a document (§5.1.3). Questions are
    **sampled and rotated** per session, which is what stops a fixed bank from
    leaking to a forum (FR-10.3).

    `company` is nullable: a null template is a platform one, which is what
    candidate practice runs against and what a company without its own
    competency framework starts from.
    """

    company = models.ForeignKey(
        "companies.Company",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="interview_templates",
        help_text="Null for a platform template, used by practice and by companies with none.",
    )
    kind = models.CharField(max_length=16, choices=InterviewKind.choices)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140)
    config = models.JSONField(default=dict, blank=True)
    question_pools = models.JSONField(
        default=dict,
        blank=True,
        help_text="Competency key to a list of questions. Sampled and rotated per session.",
    )
    rubric_version = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["company", "slug"], name="interviewtemplate_slug"),
        ]
        indexes = [
            models.Index(fields=["company", "kind"], name="interviewtemplate_company_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def competencies(self) -> list[str]:
        return sorted(self.question_pools or {})


class InterviewRound(TimeStampedModel):
    """One step of a job's interview plan (FR-10.9)."""

    job = models.ForeignKey("jobs.Job", on_delete=models.CASCADE, related_name="interview_rounds")
    template = models.ForeignKey(InterviewTemplate, on_delete=models.PROTECT, related_name="rounds")
    name = models.CharField(max_length=120)
    order = models.PositiveSmallIntegerField(default=0)
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ("order", "pk")
        constraints = [
            models.UniqueConstraint(fields=["job", "order"], name="interviewround_order_per_job"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.job_id})"


class InterviewerAvailability(TimeStampedModel):
    """When somebody is willing to interview.

    Published availability, not a calendar: this says "you may book me here",
    and `InterviewSlot` is what a booking actually lands on. Keeping them apart
    means withdrawing availability cannot silently cancel a booked interview.
    """

    membership = models.ForeignKey(
        "companies.CompanyMembership", on_delete=models.CASCADE, related_name="availability"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    recurrence = models.JSONField(default=dict, blank=True)
    timezone_name = models.CharField(max_length=64, default="Asia/Kathmandu")

    class Meta:
        ordering = ("starts_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")),
                name="availability_ends_after_start",
            ),
        ]
        indexes = [
            models.Index(fields=["membership", "starts_at"], name="availability_member_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.membership_id}: {self.starts_at:%Y-%m-%d %H:%M}"


class TsTzRange(Func):
    """`tstzrange(starts_at, ends_at)` for the exclusion constraint.

    Written out because the constraint needs the range as an *expression* over
    two columns rather than a stored range column. Storing a range instead would
    mean every query that wants a start time has to unpack one, and every
    ordering by start time becomes an expression index.
    """

    function = "tstzrange"
    output_field = DateTimeRangeField()


class InterviewSlot(TimeStampedModel):
    """A bookable window on one interviewer's calendar (FR-10.6).

    **The exclusion constraint is the feature.** Two candidates confirming the
    same slot in the same millisecond is not a rare case — it is what happens
    when a reminder goes out and everybody opens the link at once. A
    check-then-insert would let both through; Postgres rejects the second.

    `held_until` covers the seconds between choosing a slot and confirming it.
    A hold that never expires is a slot lost to somebody who closed the tab, so
    it carries a deadline and the booking service sweeps past it.
    """

    interviewer = models.ForeignKey(
        "companies.CompanyMembership", on_delete=models.CASCADE, related_name="interview_slots"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    state = models.CharField(max_length=16, choices=SlotState.choices, default=SlotState.OPEN)
    held_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="A hold expires. A slot lost to somebody who closed a tab is a slot lost.",
    )
    session = models.OneToOneField(
        "InterviewSession",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="slot",
    )

    class Meta:
        ordering = ("starts_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")),
                name="interviewslot_ends_after_start",
            ),
            # The whole point of the table. `WHERE state <> 'cancelled'` so a
            # cancelled slot frees its window rather than blocking it forever.
            ExclusionConstraint(
                name="interviewslot_no_overlap",
                expressions=[
                    ("interviewer", RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at"), RangeOperators.OVERLAPS),
                ],
                condition=~Q(state=SlotState.CANCELLED),
            ),
        ]
        indexes = [
            models.Index(fields=["interviewer", "starts_at"], name="interviewslot_member_idx"),
            models.Index(
                fields=["starts_at"],
                condition=Q(state="open"),
                name="interviewslot_open_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.interviewer_id} {self.starts_at:%Y-%m-%d %H:%M}"

    @property
    def is_held(self) -> bool:
        return (
            self.state == SlotState.HELD
            and self.held_until is not None
            and self.held_until > timezone.now()
        )

    @property
    def is_bookable(self) -> bool:
        """Open, or held by a hold that has expired."""
        if self.state == SlotState.OPEN:
            return True
        return self.state == SlotState.HELD and not self.is_held


class InterviewSession(PublicIdModel, TimeStampedModel):
    """One interview, of any kind (SRS §5.10, §11.1).

    `application` is nullable and null means **practice** (FR-10.2). That is the
    whole mechanism: a practice session has no application, so no employer
    queryset can reach it, and the candidate's own results are private by
    construction rather than by a permission check somebody might forget.
    """

    application = models.ForeignKey(
        "applications.Application",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="interview_sessions",
        help_text="Null means practice. Practice results are the candidate's alone (FR-10.2).",
    )
    candidate = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="interview_sessions"
    )
    round = models.ForeignKey(
        InterviewRound, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions"
    )
    template = models.ForeignKey(
        InterviewTemplate, on_delete=models.PROTECT, related_name="sessions"
    )
    kind = models.CharField(max_length=16, choices=InterviewKind.choices)
    state = models.CharField(
        max_length=16, choices=SessionState.choices, default=SessionState.SCHEDULED
    )

    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    provider = models.CharField(max_length=40, blank=True)
    room_ref = models.CharField(max_length=200, blank=True)
    join_token_hash = models.CharField(
        max_length=64,
        blank=True,
        help_text="Hashed. A join token is a credential, and a stored credential is a leak.",
    )

    plan = models.JSONField(
        default=dict,
        blank=True,
        help_text="Competencies and the questions sampled for this session (FR-10.3).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancelled_reason = models.CharField(max_length=200, blank=True)
    rescheduled_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reschedules"
    )
    plan_run = models.ForeignKey(
        "ai.AIRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["candidate", "-created_at"], name="session_candidate_idx"),
            models.Index(fields=["application", "-created_at"], name="session_application_idx"),
            models.Index(
                fields=["scheduled_at"],
                condition=Q(state="scheduled"),
                name="session_upcoming_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} for {self.candidate_id}"

    @property
    def is_practice(self) -> bool:
        return self.application_id is None

    @property
    def is_finished(self) -> bool:
        return self.state in SessionState.terminal()

    @property
    def duration(self) -> dt.timedelta | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    @property
    def can_join_now(self) -> bool:
        """Whether the room is open, for templates. The rule is `policies.can_join`."""
        from .policies import can_join

        return can_join(self)


class InterviewParticipant(TimeStampedModel):
    """Somebody other than the candidate who is in the room."""

    session = models.ForeignKey(
        InterviewSession, on_delete=models.CASCADE, related_name="participants"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(
        max_length=16, choices=ParticipantRole.choices, default=ParticipantRole.INTERVIEWER
    )
    attended = models.BooleanField(default=False)
    invited_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("role", "pk")
        constraints = [
            models.UniqueConstraint(fields=["session", "user"], name="participant_once_per_session")
        ]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.session_id}"


class InterviewTurn(TimeStampedModel):
    """One question and its answer (FR-10.1, FR-10.4).

    `integrity_meta` records paste and focus events. It is shown to the employer
    as context and is **never a scoring input** — typing behaviour is not job
    performance, and a rubric that scored it would be measuring the wrong thing
    with confidence. The evaluation service does not read this column, and a
    test asserts it.
    """

    session = models.ForeignKey(InterviewSession, on_delete=models.CASCADE, related_name="turns")
    order = models.PositiveSmallIntegerField()
    competency = models.CharField(max_length=60, blank=True)
    question_text = models.TextField()
    question_origin = models.CharField(
        max_length=16, choices=QuestionOrigin.choices, default=QuestionOrigin.POOL
    )
    answer_text = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    time_limit_seconds = models.PositiveIntegerField(default=0)
    integrity_meta = models.JSONField(default=dict, blank=True)
    delivery_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Spoken answers only: durations, pace, pauses and filler counts measured on the "
            "server from word timings. Feedback for the candidate; never a scoring input. "
            "The audio itself is not stored."
        ),
    )
    ai_run = models.ForeignKey(
        "ai.AIRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("order", "pk")
        constraints = [
            models.UniqueConstraint(fields=["session", "order"], name="turn_order_per_session")
        ]

    def __str__(self) -> str:
        return f"{self.session_id} #{self.order}"

    @property
    def competency_label(self) -> str:
        """The competency as a person reads it, not the slug."""
        return (self.competency or "").replace("_", " ").replace("-", " ").strip().capitalize()

    @property
    def is_answered(self) -> bool:
        return bool(self.answer_text.strip())

    @property
    def ran_over(self) -> bool:
        """Whether the answer arrived after its time box.

        Reported, never penalised: a slow connection and a slow thinker look
        identical from here, and the time box exists to shape the interview
        rather than to catch anybody.
        """
        if not self.time_limit_seconds or not self.duration_ms:
            return False
        return self.duration_ms > self.time_limit_seconds * 1000


class InterviewEvaluation(TimeStampedModel):
    """One competency, scored once, by a model or by a person (FR-10.1, FR-10.8).

    **`submitted_at` is what makes a scorecard independent.** Until it is set,
    the row is invisible to every interviewer but its author. Anchoring is the
    most studied failure in interview panels, and a `NULL` column checked by the
    selector prevents it in a way a convention does not.
    """

    session = models.ForeignKey(
        InterviewSession, on_delete=models.CASCADE, related_name="evaluations"
    )
    competency = models.CharField(max_length=60)
    score = models.PositiveSmallIntegerField(help_text="1 to 5 against the rubric.")
    rationale = models.TextField(blank=True)
    evidence = models.TextField(
        blank=True,
        help_text="The candidate's own words the score rests on, verified against the transcript.",
    )
    grounded = models.BooleanField(
        null=True,
        blank=True,
        help_text=(
            "Whether every phrase a model quoted was found in the candidate's answers. "
            "Null for a person's scorecard, which is not checked."
        ),
    )
    rubric_version = models.PositiveSmallIntegerField(default=1)
    evaluator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Null means the model scored it. Both go in the same table (§11.1).",
    )
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Hidden from other interviewers until set (FR-10.8).",
    )
    ai_run = models.ForeignKey(
        "ai.AIRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    @property
    def competency_label(self) -> str:
        """The competency as a person reads it.

        Stored as a slug because the plan is generated and the key has to be
        stable across a regeneration. Printing the slug put "problem_solving"
        on a candidate's feedback.
        """
        return (self.competency or "").replace("_", " ").replace("-", " ").strip().capitalize()

    class Meta:
        ordering = ("competency", "pk")
        constraints = [
            models.CheckConstraint(
                condition=Q(score__gte=1) & Q(score__lte=5), name="evaluation_score_in_range"
            ),
            # One score per competency per evaluator. A second is a revision,
            # and a revision that appears as a second row would be averaged in.
            #
            # `nulls_distinct=False` because the model's scores have no
            # evaluator: with NULLs distinct, as Postgres treats them by
            # default, this constraint never applied to a single AI score, and
            # two evaluations of one session running at the same moment wrote
            # two rows per competency (seen in the 2026-09-24 audit).
            models.UniqueConstraint(
                fields=["session", "competency", "evaluator"],
                name="evaluation_once_per_evaluator",
                nulls_distinct=False,
            ),
        ]
        indexes = [
            models.Index(fields=["session", "competency"], name="evaluation_session_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.competency}: {self.score}"

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None

    @property
    def by_ai(self) -> bool:
        return self.evaluator_id is None


class InterviewOutcome(TimeStampedModel):
    """What one session concluded (FR-10.10, FR-10.11).

    One per session, whatever the kind — the uniform shape §11.1 exists for.
    `shared_with_candidate` is the employer's choice and defaults to false, with
    the product rule that a rejection shares the summary living in the service
    rather than in this default.
    """

    session = models.OneToOneField(
        InterviewSession, on_delete=models.CASCADE, related_name="outcome"
    )
    recommendation = models.CharField(max_length=16, choices=Recommendation.choices)
    summary = models.TextField(blank=True)
    average_score = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    shared_with_candidate = models.BooleanField(default=False)
    shared_at = models.DateTimeField(null=True, blank=True)
    ai_run = models.ForeignKey(
        "ai.AIRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.session_id}: {self.recommendation}"

    @property
    def is_positive(self) -> bool:
        return self.recommendation in {Recommendation.STRONG_YES, Recommendation.YES}
