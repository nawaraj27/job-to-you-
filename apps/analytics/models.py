"""
Analytics (SRS §5.14, §16).

**One append-only event table and four narrow rollups.** The rule that keeps
this from becoming everything (§5.15): a fact worth *counting* writes
`AnalyticsEvent`. A state change a user must see writes `ApplicationEvent`; one
a regulator must see writes `AuditLog`; a side effect that must happen writes
`OutboxEvent`. Many changes write to several; none write to the wrong one by
accident once the rule is stated.

Two structural decisions:

  * **`AnalyticsEvent` is lossy, on purpose.** It is pruned, it is written from
    the outbox rather than inline, and losing a row costs a slightly wrong
    number rather than a broken feature. Nothing that must be correct reads it.
  * **Dashboards read rollups, never raw events.** A dashboard that aggregated
    `AnalyticsEvent` on the request path would be fine until the day a posting
    goes viral, which is the one day the employer opens it. The rollups are
    idempotent per (date, dimension), so a re-run cannot double-count.

Partition-ready per ADR-A40: no inbound foreign keys on `AnalyticsEvent`,
`created_at` in every index, converted at the 20M-row threshold rather than at a
phase boundary.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class AnalyticsEvent(models.Model):
    """One fact worth counting.

    `actor` is nullable and set null on delete: an erased account's events keep
    their shape and lose their owner, which is what §19 requires and what makes
    the aggregates survive an erasure without a recount.

    `anon_id` covers the visitor who has not signed in — most of the top of the
    funnel — and is a rotating browser identifier, never an IP or a fingerprint.
    """

    name = models.CharField(max_length=80, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    anon_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="A rotating browser identifier for signed-out visitors. Never an IP.",
    )
    company = models.ForeignKey(
        "companies.Company",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Denormalised so a company's rollup is one indexed scan rather than a join.",
    )
    props = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=["name", "-created_at"], name="analyticsevent_name_idx"),
            models.Index(fields=["company", "-created_at"], name="analyticsevent_company_idx"),
            models.Index(fields=["actor", "-created_at"], name="analyticsevent_actor_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} at {self.created_at:%Y-%m-%d %H:%M}"


class DailyStat(models.Model):
    """Shared shape for the rollups.

    `computed_at` rather than `created_at`: these rows are recomputed in place
    by an idempotent job, so "when the row appeared" is not the useful fact —
    "when this number was last worked out" is.
    """

    day = models.DateField(db_index=True)
    computed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        abstract = True
        ordering = ("-day",)


class JobDailyStat(DailyStat):
    """One posting's day (SRS §16.2, employer view).

    Views, applies and shortlists in one row so "views → applies → shortlists"
    is one query rather than three. The conversion rates are properties rather
    than columns: a stored ratio is a number that goes stale the moment either
    side of it is corrected.
    """

    job = models.ForeignKey("jobs.Job", on_delete=models.CASCADE, related_name="daily_stats")
    views = models.PositiveIntegerField(default=0)
    unique_viewers = models.PositiveIntegerField(default=0)
    applications = models.PositiveIntegerField(default=0)
    shortlisted = models.PositiveIntegerField(default=0)
    rejected = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = False
        ordering = ("-day",)
        constraints = [
            models.UniqueConstraint(fields=["job", "day"], name="jobdailystat_once_per_day"),
        ]
        indexes = [models.Index(fields=["job", "-day"], name="jobdailystat_job_idx")]

    def __str__(self) -> str:
        return f"{self.job_id} on {self.day}"

    @property
    def apply_rate(self) -> float:
        """Applications per view. The number an employer actually asks for."""
        return round(self.applications / self.views, 4) if self.views else 0.0

    @property
    def shortlist_rate(self) -> float:
        return round(self.shortlisted / self.applications, 4) if self.applications else 0.0


class CompanyDailyStat(DailyStat):
    """One company's day.

    `response_*` is §16.3's third steering measurement: employer response rate
    and time. It is computed here rather than live because it is the number a
    nudge and later a public badge both read, and a badge that recomputes on
    every page view is a badge that gets switched off.
    """

    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="daily_stats"
    )
    applications = models.PositiveIntegerField(default=0)
    responded = models.PositiveIntegerField(
        default=0, help_text="Applications that moved out of `new` on this day."
    )
    response_hours_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text="Summed so the average survives a re-run without keeping every row.",
    )
    interviews = models.PositiveIntegerField(default=0)
    offers = models.PositiveIntegerField(default=0)
    hires = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = False
        ordering = ("-day",)
        constraints = [
            models.UniqueConstraint(
                fields=["company", "day"], name="companydailystat_once_per_day"
            ),
        ]
        indexes = [models.Index(fields=["company", "-day"], name="companydailystat_idx")]

    def __str__(self) -> str:
        return f"{self.company_id} on {self.day}"

    @property
    def response_rate(self) -> float:
        return round(self.responded / self.applications, 4) if self.applications else 0.0

    @property
    def average_response_hours(self) -> float:
        if not self.responded:
            return 0.0
        return round(float(self.response_hours_total) / self.responded, 1)


class FunnelSnapshot(DailyStat):
    """How many applications sit in each stage, per company, per day.

    A snapshot rather than a movement log: "where is everybody now" is the
    question a funnel answers, and deriving it from movements would mean
    replaying every transition to draw one chart.

    `stage_key` rather than a foreign key to `PipelineStage`, because a company
    that renames or deletes a stage must not lose its own history — the number
    of people who were in "screening" last March is still true.
    """

    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="funnel_snapshots"
    )
    stage_key = models.CharField(max_length=40)
    stage_order = models.PositiveSmallIntegerField(default=0)
    count = models.PositiveIntegerField(default=0)
    entered = models.PositiveIntegerField(
        default=0, help_text="Applications that arrived in this stage on this day."
    )
    exited = models.PositiveIntegerField(default=0)
    hours_in_stage_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )

    class Meta:
        abstract = False
        ordering = ("-day", "stage_order")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "day", "stage_key"], name="funnelsnapshot_once_per_stage"
            ),
        ]
        indexes = [models.Index(fields=["company", "-day"], name="funnelsnapshot_company_idx")]

    def __str__(self) -> str:
        return f"{self.company_id} {self.stage_key} on {self.day}"

    @property
    def average_hours_in_stage(self) -> float:
        return round(float(self.hours_in_stage_total) / self.exited, 1) if self.exited else 0.0


class AICostDaily(DailyStat):
    """What AI cost, per day, per task (SRS §16.2, platform view).

    Per task rather than per company: the question this answers is "which
    feature is expensive", and a company-level total cannot answer it. Per-owner
    spend is already enforced live by `AIUsageQuota` (§5.11), which is a budget
    rather than a report.
    """

    task_key = models.CharField(max_length=60)
    runs = models.PositiveIntegerField(default=0)
    failures = models.PositiveIntegerField(default=0)
    cached = models.PositiveIntegerField(default=0)
    tokens_in = models.PositiveBigIntegerField(default=0)
    tokens_out = models.PositiveBigIntegerField(default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    latency_ms_total = models.PositiveBigIntegerField(default=0)

    class Meta:
        abstract = False
        ordering = ("-day", "task_key")
        constraints = [
            models.UniqueConstraint(fields=["day", "task_key"], name="aicostdaily_once_per_task"),
        ]

    def __str__(self) -> str:
        return f"{self.task_key} on {self.day}"

    @property
    def failure_rate(self) -> float:
        return round(self.failures / self.runs, 4) if self.runs else 0.0

    @property
    def cache_rate(self) -> float:
        return round(self.cached / self.runs, 4) if self.runs else 0.0

    @property
    def average_latency_ms(self) -> int:
        return round(self.latency_ms_total / self.runs) if self.runs else 0


class SteeringMetric(models.TextChoices):
    """The four measurements that steer the product (SRS §16.3).

    Named in an enum rather than left as strings because §16.3 says these four
    decide whether the product's central claims are true, and a fifth appearing
    by typo would dilute that without anybody noticing.
    """

    MATCH_PREDICTS_OUTCOME = "match_predicts_outcome", _("Match score against outcome")
    AI_SUGGESTIONS_ACCEPTED = "ai_suggestions_accepted", _("AI suggestions accepted vs offered")
    EMPLOYER_RESPONSE = "employer_response", _("Employer response rate and time")
    INTERVIEW_PREDICTS_HIRE = "interview_predicts_hire", _("Interview outcome against hire")


class SteeringSnapshot(DailyStat):
    """One of the four measurements, on one day (§16.3).

    Stored rather than computed live because these are read on a staff screen
    and compared over months — a chart of "did the matcher get better" needs the
    old numbers as they were, not as today's scorer would compute them.

    `sample_size` travels with every value. A correlation over eleven
    applications is not a finding, and a number shown without its sample size
    invites somebody to act on eleven applications.
    """

    metric = models.CharField(max_length=40, choices=SteeringMetric.choices)
    value = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    sample_size = models.PositiveIntegerField(default=0)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = False
        ordering = ("-day", "metric")
        constraints = [
            models.UniqueConstraint(
                fields=["day", "metric"], name="steeringsnapshot_once_per_metric"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.metric} on {self.day}: {self.value}"

    @property
    def is_meaningful(self) -> bool:
        """Whether the sample is large enough to say anything.

        Thirty, which is not a statistical law but is the point below which a
        product decision made on this number is a decision made on noise.
        """
        return self.sample_size >= 30
