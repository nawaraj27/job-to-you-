"""
Jobs (SRS §5.7, ADR-A23).

A job is the employer's half of the marketplace, and almost everything about
this table exists because of a failure a job board has when it is missing:

  * **`remote_scope` answers "remote from where".** `workplace='remote'` alone
    does not, and its absence is the most common complaint on every job board
    ever built. A `CHECK` makes a remote job without a scope impossible.
  * **Salary is stored twice** — what the employer typed, and an annualised
    base with its FX provenance (ADR-A39) — because a job in NPR per month and
    a candidate expecting USD per year cannot be compared until both are.
  * **`row_version` is an optimistic lock.** Two recruiters editing one posting
    is normal in a team, and a silent lost update is how a salary band or a
    closing date quietly reverts.
  * **There is no `JobRevision` table (A23).** The requirement it existed for —
    an applicant seeing what they applied to — is met by `Application.job_snapshot`
    in P8: scoped to the row that needs it, and unable to drift.

`search_vector` is a generated column so it can never disagree with the row it
describes; a trigger or a save() hook is a second place for the truth to live.
"""

from __future__ import annotations

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from django.db.models.functions import Now
from django.utils.translation import gettext_lazy as _
from pgvector.django import HnswIndex

from apps.common.models import EmbeddedModel, PublicIdModel, TimeStampedModel


class JobStatus(models.TextChoices):
    """Where a posting is in its life (SRS §7.4).

    Five states, and the distinctions are all load-bearing. `closed` is a role
    that was filled or abandoned; `expired` is one nobody closed and the
    deadline passed; `archived` is out of the way but readable. Collapsing any
    pair loses the reason a posting stopped accepting people.
    """

    DRAFT = "draft", _("Draft")
    PUBLISHED = "published", _("Published")
    # FR-5.1 and §7.5: taken off the site for now, and coming back. Not closed —
    # nobody was hired and nothing was cancelled — and not a draft, which
    # would lose the publication date and re-fire every alert on its return.
    PAUSED = "paused", _("Paused")
    CLOSED = "closed", _("Closed")
    EXPIRED = "expired", _("Expired")
    ARCHIVED = "archived", _("Archived")


#: Statuses a candidate may ever see. Everything else is company-internal, and
#: this tuple is what every public read filters on.
PUBLIC_STATUSES = (JobStatus.PUBLISHED,)


class Workplace(models.TextChoices):
    ON_SITE = "on_site", _("On-site")
    HYBRID = "hybrid", _("Hybrid")
    REMOTE = "remote", _("Remote")


class RemoteScope(models.TextChoices):
    """Remote from *where* — the question `workplace` cannot answer."""

    NONE = "none", _("Not remote")
    CITY = "city", _("Within the city")
    COUNTRY = "country", _("Anywhere in the country")
    REGION = "region", _("Anywhere in the region")
    WORLDWIDE = "worldwide", _("Anywhere in the world")


class EmploymentType(models.TextChoices):
    FULL_TIME = "full_time", _("Full-time")
    PART_TIME = "part_time", _("Part-time")
    CONTRACT = "contract", _("Contract")
    INTERNSHIP = "internship", _("Internship")
    TEMPORARY = "temporary", _("Temporary")


class ExperienceLevel(models.TextChoices):
    INTERN = "intern", _("Intern")
    ENTRY = "entry", _("Entry level")
    MID = "mid", _("Mid level")
    SENIOR = "senior", _("Senior")
    LEAD = "lead", _("Lead or principal")
    EXECUTIVE = "executive", _("Executive")


class ApplyMode(models.TextChoices):
    # Brand-free on purpose (docs/BRANDING.md); contrasts with EXTERNAL below.
    PLATFORM = "platform", _("Apply directly")
    EXTERNAL = "external", _("Apply on the company's site")


class JobQuerySet(models.QuerySet):
    def alive(self) -> JobQuerySet:
        return self.filter(deleted_at__isnull=True)

    def for_company(self, company) -> JobQuerySet:
        """Every read inside the employer portal starts here (ADR-A18)."""
        return self.alive().filter(company=company)

    def published(self) -> JobQuerySet:
        """What a candidate may see.

        Suspended companies are excluded here rather than by every caller: a
        suspension that leaves jobs reachable is not a suspension (SRS §2.6).
        """
        from apps.companies.models import CompanyState

        return self.alive().filter(status=JobStatus.PUBLISHED, company__state=CompanyState.ACTIVE)

    def expiring(self) -> JobQuerySet:
        """Published jobs whose closing date has passed."""
        return self.alive().filter(
            status=JobStatus.PUBLISHED, closes_at__isnull=False, closes_at__lte=Now()
        )


class Job(PublicIdModel, EmbeddedModel, TimeStampedModel):
    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="jobs")
    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=160)
    category = models.ForeignKey(
        "taxonomy.JobCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )

    description = models.TextField(blank=True)
    responsibilities = models.JSONField(default=list, blank=True)
    requirements = models.JSONField(default=list, blank=True)
    benefits = models.JSONField(default=list, blank=True)

    location = models.ForeignKey(
        "taxonomy.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs"
    )
    workplace = models.CharField(
        max_length=16, choices=Workplace.choices, default=Workplace.ON_SITE
    )
    remote_scope = models.CharField(
        max_length=16, choices=RemoteScope.choices, default=RemoteScope.NONE
    )
    remote_countries = models.JSONField(
        default=list, blank=True, help_text="ISO 3166-1 alpha-2 codes a remote hire may work from."
    )

    employment_type = models.CharField(
        max_length=16, choices=EmploymentType.choices, default=EmploymentType.FULL_TIME
    )
    experience_level = models.CharField(
        max_length=16, choices=ExperienceLevel.choices, default=ExperienceLevel.MID
    )

    salary_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    period = models.CharField(max_length=8, blank=True)
    salary_min_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    salary_max_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    fx_rate_id = models.PositiveBigIntegerField(null=True, blank=True)
    fx_rate_value = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    fx_as_of = models.DateField(null=True, blank=True)
    fx_source = models.CharField(max_length=60, blank=True)
    salary_visible = models.BooleanField(
        default=True,
        help_text="Hiding pay costs applications. The platform's default is to show it.",
    )

    status = models.CharField(max_length=16, choices=JobStatus.choices, default=JobStatus.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(max_length=200, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    apply_mode = models.CharField(
        max_length=16, choices=ApplyMode.choices, default=ApplyMode.PLATFORM
    )
    external_url = models.URLField(blank=True)
    application_cooldown_days = models.PositiveSmallIntegerField(
        default=0, help_text="How long before a rejected candidate may apply again."
    )

    views_count = models.PositiveIntegerField(default=0)
    applications_count = models.PositiveIntegerField(default=0)
    row_version = models.PositiveIntegerField(default=0)

    # Generated by Postgres, never by Python. A trigger or a save() hook would
    # be a second place for the truth to live, and the failure mode is a job
    # that is unfindable by the words printed on it.
    search_vector = models.GeneratedField(
        expression=SearchVector("title", "description", config="english"),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = JobQuerySet.as_manager()

    class Meta:
        ordering = ("-published_at", "-created_at")
        indexes = [
            GinIndex(fields=["search_vector"], name="job_search_idx"),
            # Partial, because nearly every vector query also filters on
            # published (ADR-A24). An index over draft and closed postings would
            # be larger, slower to build, and read by nothing.
            HnswIndex(
                name="job_embedding_idx",
                fields=["embedding"],
                condition=models.Q(status="published"),
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
            models.Index(
                fields=["status", "-published_at"],
                condition=models.Q(status="published"),
                name="job_published_idx",
            ),
            models.Index(fields=["company", "status"], name="job_company_status_idx"),
            models.Index(
                fields=["closes_at"],
                condition=models.Q(status="published", closes_at__isnull=False),
                name="job_expiry_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(salary_min__isnull=True)
                    | models.Q(salary_max__isnull=True)
                    | models.Q(salary_max__gte=models.F("salary_min"))
                ),
                name="job_salary_range_ordered",
            ),
            # A published job has a publication date. Without this, "published"
            # is a word rather than a fact, and every ordering by it is wrong.
            models.CheckConstraint(
                condition=(~models.Q(status="published") | models.Q(published_at__isnull=False)),
                name="job_published_has_date",
            ),
            # Remote from where. The check is the point of the column.
            models.CheckConstraint(
                condition=(~models.Q(workplace="remote") | ~models.Q(remote_scope="none")),
                name="job_remote_has_scope",
            ),
            models.CheckConstraint(
                condition=(~models.Q(apply_mode="external") | ~models.Q(external_url="")),
                name="job_external_has_url",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("jobs:detail", args=[self.slug])

    def as_snapshot(self) -> dict:
        """The posting as it reads right now, frozen onto an application.

        This is what makes A23's removal of `JobRevision` safe: an applicant
        sees what they applied to, from the row that needs it, with no second
        table to keep in step. Copied by value on purpose — a snapshot holding
        ids would resolve to today's posting, which is the bug it exists to
        prevent.
        """
        return {
            "title": self.title,
            "slug": self.slug,
            "company_name": self.company.name,
            "description": self.description,
            "responsibilities": list(self.responsibilities or []),
            "requirements": list(self.requirements or []),
            "benefits": list(self.benefits or []),
            "workplace": self.workplace,
            "remote_scope": self.remote_scope,
            "employment_type": self.employment_type,
            "experience_level": self.experience_level,
            "location": self.location.name if self.location is not None else "",
            "salary_min": str(self.salary_min) if self.salary_min is not None else "",
            "salary_max": str(self.salary_max) if self.salary_max is not None else "",
            "currency": self.currency,
            "period": self.period,
            "salary_visible": self.salary_visible,
            "skills": [link.skill.name for link in self.skills.all()],
            "published_at": self.published_at.isoformat() if self.published_at else "",
        }

    @property
    def is_published(self) -> bool:
        return self.status == JobStatus.PUBLISHED

    @property
    def is_open(self) -> bool:
        """Accepting applications. Read by P8 before it accepts one.

        A suspended company's postings are not open either (§2.6). Checked here
        and not only in `published()`, because a saved row or an apply form
        holds the job instance and asks it directly.
        """
        from apps.companies.models import CompanyState

        return (
            self.is_published
            and self.deleted_at is None
            and self.company.state == CompanyState.ACTIVE
        )

    @property
    def is_editable(self) -> bool:
        return (
            self.status in {JobStatus.DRAFT, JobStatus.PUBLISHED, JobStatus.PAUSED}
            and self.deleted_at is None
        )


class Requirement(models.TextChoices):
    REQUIRED = "required", _("Required")
    PREFERRED = "preferred", _("Preferred")


class JobSkill(TimeStampedModel):
    """What the matcher will score against (P13). Never free text."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="skills")
    skill = models.ForeignKey("taxonomy.Skill", on_delete=models.PROTECT, related_name="+")
    requirement = models.CharField(
        max_length=16, choices=Requirement.choices, default=Requirement.REQUIRED
    )
    weight = models.DecimalField(max_digits=3, decimal_places=2, default=1)
    min_years = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("requirement", "order")
        constraints = [
            models.UniqueConstraint(fields=["job", "skill"], name="jobskill_once_per_job")
        ]
        indexes = [models.Index(fields=["skill"], name="jobskill_skill_idx")]

    def __str__(self) -> str:
        return f"{self.job_id}: {self.skill_id}"


class JobAssignment(TimeStampedModel):
    """Who owns a posting inside the company (SRS §2.3).

    One table with a `role` rather than `JobRecruiter` and `JobHiringManager`
    as separate models: the two differ only in what the assignment *means*, and
    two tables with identical columns drift the moment one gains a field.

    Assignment is what makes write access work for a recruiter who does not
    hold `job.write_any` — they may edit the postings they were given.
    """

    class Kind(models.TextChoices):
        RECRUITER = "recruiter", _("Recruiter")
        HIRING_MANAGER = "hiring_manager", _("Hiring manager")

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="assignments")
    membership = models.ForeignKey(
        "companies.CompanyMembership", on_delete=models.CASCADE, related_name="job_assignments"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.RECRUITER)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "membership", "kind"], name="jobassignment_once_per_role"
            )
        ]
        indexes = [models.Index(fields=["membership"], name="jobassignment_member_idx")]

    def __str__(self) -> str:
        return f"{self.membership_id} on {self.job_id} as {self.kind}"


class QuestionKind(models.TextChoices):
    SHORT_TEXT = "short_text", _("Short answer")
    LONG_TEXT = "long_text", _("Long answer")
    SINGLE_CHOICE = "single_choice", _("Choose one")
    MULTI_CHOICE = "multi_choice", _("Choose several")
    BOOLEAN = "boolean", _("Yes or no")
    NUMBER = "number", _("Number")


class JobScreeningQuestion(TimeStampedModel):
    """A question every applicant answers.

    `knockout` describes the answer that disqualifies. Stored rather than
    applied here: P8 owns what happens to an application, and a model that
    rejected people would be business logic in the wrong layer.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="questions")
    order = models.PositiveSmallIntegerField(default=0)
    kind = models.CharField(max_length=16, choices=QuestionKind.choices)
    text = models.CharField(max_length=300)
    options = models.JSONField(default=list, blank=True)
    is_required = models.BooleanField(default=True)
    knockout = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ("order", "pk")

    def __str__(self) -> str:
        return self.text[:60]


class JobView(models.Model):
    """One read of a posting (SRS §5.7).

    `viewer_hash` rather than an IP: the count needs to know "was this the same
    person again", not who they were. Partition-ready per ADR-A40 — no inbound
    foreign keys, `created_at` in the index — and rolled up into
    `Job.views_count` rather than counted live.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="views")
    viewer_hash = models.CharField(max_length=64, db_index=True)
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    referrer = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["job", "-created_at"], name="jobview_job_idx")]

    def __str__(self) -> str:
        return f"view of {self.job_id}"


class JobAlertMatch(models.Model):
    """A job an alert decided to tell someone about (ADR-A21).

    The only persisted matching artefact in the system. Alerts are genuinely
    batch work with a fixed recipient set — a saved search — so the result is
    worth storing; recommendations are computed on read and are not.

    `score` here is search relevance, not the P13 matcher: this phase has no
    matcher, and a column that pretended otherwise would be read as one later.
    """

    saved_search = models.ForeignKey(
        "candidates.SavedSearch", on_delete=models.CASCADE, related_name="matches"
    )
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="alert_matches")
    score = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    computed_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-score", "-computed_at")
        constraints = [
            models.UniqueConstraint(
                fields=["saved_search", "job"], name="jobalertmatch_once_per_search"
            )
        ]
        indexes = [
            models.Index(
                fields=["saved_search", "-computed_at"],
                condition=models.Q(notified_at__isnull=True),
                name="jobalertmatch_pending_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.job_id} for search {self.saved_search_id}"
