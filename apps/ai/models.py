"""
The record of what the AI actually did (SRS §5.11, ADR-A11, ADR-A20).

Four tables, and only four, because provider and routing configuration lives in
code (ADR-A28). What must be in the database is what happened:

  * **`AIRun` — every output, always.** Without it there is no cache, no cost
    attribution, no reproducibility, no evaluation corpus and no answer to
    "what produced this". One row per call buys all five.
  * **`PromptTemplate` — the identity of the text that ran.** The body lives in
    version control; this records its hash so `AIRun.prompt_version` means
    something six months later.
  * **`AIFeedback` — the eval corpus, collected from day one.** A thumbs-down
    on the day somebody is annoyed is worth more than a labelling exercise
    six months later.
  * **`AIUsageQuota` — money, checked before dispatch.** Never after: a limit
    enforced after the spend is an audit trail, not a limit.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import PublicIdModel, TimeStampedModel


class PromptTemplate(TimeStampedModel):
    """One version of one prompt, loaded from a file at deploy (ADR-A20).

    The body is stored so a run can be reproduced exactly even after the file
    changes, and `body_sha256` is what proves the stored copy is the file that
    was reviewed. A prompt editable in an admin screen is a prompt changed at
    2 a.m. with no diff, no test and no rollback — while behaving exactly like
    code, because it determines the output.
    """

    task_key = models.CharField(max_length=60)
    version = models.PositiveIntegerField()
    body = models.TextField()
    body_sha256 = models.CharField(max_length=64)
    output_schema = models.JSONField(
        default=dict,
        blank=True,
        help_text="What a valid answer looks like. Validated on every run.",
    )
    source_path = models.CharField(max_length=300)
    loaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("task_key", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=["task_key", "version"], name="prompttemplate_version_per_task"
            )
        ]
        indexes = [models.Index(fields=["task_key", "-version"], name="prompttemplate_task_idx")]

    def __str__(self) -> str:
        return f"{self.task_key} v{self.version}"

    @property
    def short_hash(self) -> str:
        return self.body_sha256[:12]


class RunStatus(models.TextChoices):
    """Why a run ended, and — through `is_retryable` — whether trying again
    could possibly help.

    The distinction is the whole point of classifying at all: a timeout is
    worth another attempt on another provider, a schema error is the same
    prompt producing the same shape and will fail identically forever.
    """

    PENDING = "pending", _("Running")
    OK = "ok", _("Succeeded")
    SCHEMA_ERROR = "schema_error", _("Output did not match the schema")
    PROVIDER_ERROR = "provider_error", _("The provider failed")
    TIMEOUT = "timeout", _("The provider timed out")
    QUOTA_EXCEEDED = "quota_exceeded", _("Out of budget")
    REFUSED = "refused", _("The model declined")
    #: The kill switch was off. Its own status rather than `provider_error`,
    #: because the two look identical on a dashboard and are opposites: one is
    #: a provider having a bad afternoon, the other is us having switched AI off
    #: on purpose. Folding a deliberate shutdown into the provider error rate
    #: fires the alert during the incident you already know about and hides the
    #: real provider failures underneath it (DEVELOPER.md §5.9).
    DISABLED = "disabled", _("AI is switched off")
    #: The subject is being erased (SRS §19.4). Its own status for the same
    #: reason as `disabled`: it is not a failure, and counting it as one would
    #: put a privacy request on the provider error-rate graph.
    ERASED = "erased", _("The subject is being erased")
    #: We decided not to ask. The assistant builder declines a message that is
    #: an instruction to it rather than an account of somebody's work, and
    #: answers it without a model. Its own status for the same reason as the
    #: two above: `refused` means the model said no, this means it was never
    #: asked, and a provider dashboard that cannot tell them apart is wrong
    #: about both.
    NOT_ASKED = "not_asked", _("Not sent to a model")

    @classmethod
    def retryable(cls) -> frozenset[str]:
        """Transient. Another attempt, ideally elsewhere, may work."""
        return frozenset({cls.PROVIDER_ERROR, cls.TIMEOUT})

    @classmethod
    def terminal(cls) -> frozenset[str]:
        """Permanent. Retrying spends money to fail identically.

        `DISABLED` is here because nothing the runner can do changes it: the
        flag goes back on by hand, and a caller looping on it would spin.
        `ERASED` is terminal in the strongest sense — retrying would be the
        exact recreation of data the erasure exists to prevent.
        """
        return frozenset(
            {
                cls.OK,
                cls.SCHEMA_ERROR,
                cls.QUOTA_EXCEEDED,
                cls.REFUSED,
                cls.DISABLED,
                cls.NOT_ASKED,
                cls.ERASED,
            }
        )


class AIRunQuerySet(models.QuerySet):
    def successful(self) -> AIRunQuerySet:
        return self.filter(status=RunStatus.OK)

    def fresh(self, *, task_key: str, prompt_version: int, input_hash: str, since):
        """The cache lookup (ADR-A11).

        Keyed on the prompt version as well as the input, so a prompt change
        invalidates every cached answer it produced — which is the behaviour a
        prompt change has to have, or an improvement never reaches anybody.
        """
        return (
            self.successful()
            .filter(
                task_key=task_key,
                prompt_version=prompt_version,
                input_hash=input_hash,
                created_at__gte=since,
                cached_from__isnull=True,
            )
            .order_by("-created_at")
        )


class AIRun(PublicIdModel, TimeStampedModel):
    task_key = models.CharField(max_length=60)
    prompt_version = models.PositiveIntegerField(default=0)
    provider = models.CharField(max_length=40)
    model = models.CharField(max_length=120)

    input_hash = models.CharField(max_length=64, db_index=True)
    input_ref_ct = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    input_ref_id = models.CharField(max_length=64, blank=True)
    input_ref = GenericForeignKey("input_ref_ct", "input_ref_id")
    input_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "What went in. Carries personal data and inherits the retention of "
            "the record it came from (ADR-A11)."
        ),
    )

    output = models.JSONField(default=dict, blank=True)
    output_schema_version = models.PositiveSmallIntegerField(default=1)
    raw_output = models.TextField(
        blank=True,
        help_text="Exactly what came back, before parsing. The only way to debug a schema error.",
    )

    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.PENDING)
    error = models.CharField(max_length=500, blank=True)

    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    latency_ms = models.PositiveIntegerField(default=0)
    attempt = models.PositiveSmallIntegerField(default=1)

    cached_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="cache_hits"
    )
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="ai_runs"
    )
    company = models.ForeignKey(
        "companies.Company",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_runs",
    )

    objects = AIRunQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [
            # The cache lookup.
            models.Index(
                fields=["task_key", "prompt_version", "input_hash"], name="airun_cache_idx"
            ),
            # Cost attribution, per side.
            models.Index(fields=["user", "-created_at"], name="airun_user_idx"),
            models.Index(fields=["company", "-created_at"], name="airun_company_idx"),
            models.Index(fields=["status", "-created_at"], name="airun_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.task_key} on {self.model} ({self.status})"

    @property
    def succeeded(self) -> bool:
        return self.status == RunStatus.OK

    @property
    def was_cached(self) -> bool:
        return self.cached_from_id is not None

    @property
    def is_retryable(self) -> bool:
        return self.status in RunStatus.retryable()


class Verdict(models.TextChoices):
    GOOD = "good", _("Useful")
    BAD = "bad", _("Not useful")


class AIFeedback(TimeStampedModel):
    """A person's verdict on one output.

    The evaluation corpus, gathered from the first day rather than assembled
    later: a thumbs-down collected at the moment somebody was annoyed carries
    information that a labelling exercise six months afterwards does not.
    """

    run = models.ForeignKey(AIRun, on_delete=models.CASCADE, related_name="feedback")
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    verdict = models.CharField(max_length=8, choices=Verdict.choices)
    comment = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["run", "user"], name="aifeedback_one_per_person")
        ]

    def __str__(self) -> str:
        return f"{self.verdict} on {self.run_id}"


class QuotaPeriod(models.TextChoices):
    DAY = "day", _("Daily")
    MONTH = "month", _("Monthly")


class AIUsageQuota(TimeStampedModel):
    """What one owner has spent, and what they may spend.

    Checked **before** dispatch, never after. A limit enforced after the money
    is gone is an audit trail wearing a limit's name.

    Generic owner because a user and a company both have budgets and neither is
    a special case of the other.
    """

    owner_ct = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    owner_id = models.CharField(max_length=64)
    owner = GenericForeignKey("owner_ct", "owner_id")

    period = models.CharField(max_length=8, choices=QuotaPeriod.choices, default=QuotaPeriod.MONTH)
    period_start = models.DateField()

    runs = models.PositiveIntegerField(default=0)
    tokens = models.PositiveBigIntegerField(default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    limit_cost = models.DecimalField(max_digits=12, decimal_places=6)

    class Meta:
        ordering = ("-period_start",)
        constraints = [
            models.UniqueConstraint(
                fields=["owner_ct", "owner_id", "period", "period_start"],
                name="aiusagequota_one_per_period",
            )
        ]
        indexes = [
            models.Index(fields=["owner_ct", "owner_id", "-period_start"], name="aiquota_owner_idx")
        ]

    def __str__(self) -> str:
        return f"{self.owner_id} {self.period} from {self.period_start}"

    @property
    def remaining(self) -> Decimal:
        return max(self.limit_cost - self.cost, Decimal("0"))

    @property
    def is_exhausted(self) -> bool:
        return self.cost >= self.limit_cost
