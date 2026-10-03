"""
The candidate domain (SRS §5.4, ADR-A22).

`CandidateProfile` is the aggregate root. Every candidate-side relation in the
system points at it, never at `User` — which stays the authentication identity
and holds nothing about employability. That split is what makes erasure clean:
anonymise one row and every foreign key still resolves.

Two things here are load-bearing beyond their size:

  * **`CandidateSkill` points at a `Skill`, never at free text.** It is the
    table the matcher reads. A free-text column would make "Python", "python3"
    and "Pyhton" three different requirements and quietly break every score
    (SRS §5.4, ADR-A13).
  * **Salary is stored twice on purpose.** The figures the candidate typed are
    what the interface shows; the annualised base figures are what comparison
    uses, and they carry the FX rate that produced them so a match made last
    March is still explicable this March (ADR-A39).
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from pgvector.django import HnswIndex

from apps.common.crypto import EncryptedCharField
from apps.common.models import EmbeddedModel, PublicIdModel, TimeStampedModel


class Visibility(models.TextChoices):
    """Who may read this profile (SRS §2.7).

    `NOBODY` does not mean "switched off": matching still runs, so the
    candidate keeps seeing jobs while remaining invisible to employers. A
    candidate who has to choose between privacy and usefulness will choose
    usefulness and then resent it.
    """

    EVERYONE = "everyone", _("Anyone with the link")
    EMPLOYERS = "employers", _("Employers you've applied to")
    NOBODY = "nobody", _("Nobody")


class Seeking(models.TextChoices):
    ACTIVE = "active", _("Actively looking")
    OPEN = "open", _("Open to offers")
    NOT_LOOKING = "not_looking", _("Not looking")


class WorkType(models.TextChoices):
    """What a candidate will accept. Stored as a list on the profile."""

    FULL_TIME = "full_time", _("Full-time")
    PART_TIME = "part_time", _("Part-time")
    CONTRACT = "contract", _("Contract")
    INTERNSHIP = "internship", _("Internship")
    REMOTE = "remote", _("Remote")
    HYBRID = "hybrid", _("Hybrid")
    ON_SITE = "on_site", _("On-site")


class SkillSource(models.TextChoices):
    SELF = "self", _("Added by the candidate")
    RESUME = "resume", _("Extracted from a resume")
    INFERRED = "inferred", _("Inferred")


class SkillLevel(models.IntegerChoices):
    BEGINNER = 1, _("Beginner")
    WORKING = 2, _("Working knowledge")
    PROFICIENT = 3, _("Proficient")
    ADVANCED = 4, _("Advanced")
    EXPERT = 5, _("Expert")


class LinkKind(models.TextChoices):
    WEBSITE = "website", _("Website")
    LINKEDIN = "linkedin", _("LinkedIn")
    GITHUB = "github", _("GitHub")
    PORTFOLIO = "portfolio", _("Portfolio")
    OTHER = "other", _("Other")


class LinkStatus(models.TextChoices):
    UNCHECKED = "unchecked", _("Not checked")
    OK = "ok", _("Reachable")
    BROKEN = "broken", _("Broken")


class Proficiency(models.TextChoices):
    """CEFR, because it is the scale employers and candidates both already use."""

    A1 = "a1", _("A1 (beginner)")
    A2 = "a2", _("A2 (elementary)")
    B1 = "b1", _("B1 (intermediate)")
    B2 = "b2", _("B2 (upper intermediate)")
    C1 = "c1", _("C1 (advanced)")
    C2 = "c2", _("C2 (proficient)")
    NATIVE = "native", _("Native")


class CandidateProfileQuerySet(models.QuerySet):
    def searchable(self) -> CandidateProfileQuerySet:
        """Profiles an employer-side search may return.

        `is_searchable` is derived, not chosen: it is false while the profile
        is too thin to represent the person fairly. Ranking an empty profile
        against a real one wastes both sides' time.
        """
        return self.filter(is_searchable=True).exclude(visibility=Visibility.NOBODY)


class CandidateProfile(PublicIdModel, EmbeddedModel, TimeStampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="candidate_profile"
    )

    headline = models.CharField(max_length=160, blank=True)
    summary = models.TextField(blank=True)
    location = models.ForeignKey(
        "taxonomy.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    phone = EncryptedCharField(
        max_length=40,
        blank=True,
        default="",
        help_text=(
            "Encrypted at rest (DEVELOPER.md §6.11). Never shown to an employer before "
            "the application stage that reveals contact details (ADR-A34)."
        ),
    )

    notice_period_days = models.PositiveSmallIntegerField(null=True, blank=True)
    seeking = models.CharField(max_length=16, choices=Seeking.choices, blank=True)
    open_to = models.JSONField(
        default=list, blank=True, help_text="WorkType values the candidate will accept."
    )

    salary_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    period = models.CharField(max_length=8, blank=True)

    # Comparison figures. Derived; never typed by anyone (ADR-A39).
    salary_min_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    salary_max_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    fx_rate_id = models.PositiveBigIntegerField(null=True, blank=True)
    fx_rate_value = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    fx_as_of = models.DateField(null=True, blank=True)
    fx_source = models.CharField(max_length=60, blank=True)

    visibility = models.CharField(
        max_length=16, choices=Visibility.choices, default=Visibility.EMPLOYERS
    )
    allow_contact = models.BooleanField(default=True)
    show_salary = models.BooleanField(default=False)
    search_indexing = models.BooleanField(
        default=False, help_text="Public profile only: allow search engines to index it."
    )

    completeness = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    is_searchable = models.BooleanField(default=False)

    #: Mirrors `User.erasure_started_at` (SRS §19.4). Duplicated deliberately:
    #: the AI and analysis tasks that must abort hold a profile, not a user, and
    #: a join per task entry to answer "is this being erased" is a join on the
    #: hottest path in the system.
    erasure_started_at = models.DateTimeField(null=True, blank=True)
    experience_months = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Distinct months of work history, overlaps counted once. Derived like "
            "`completeness` and maintained on the same path — stored rather than "
            "computed per query because talent search filters and orders on it, and "
            "deriving it in SQL would be a second implementation of the arithmetic "
            "`apps.common.periods` owns (P15)."
        ),
    )

    row_version = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Incremented on every write. Two tabs editing one profile is the normal "
            "case, not the exotic one, and a lost update there is silent."
        ),
    )

    # Generated by Postgres, never by Python (ADR-A51). A trigger or a save()
    # hook is bypassed by `queryset.update()`, and the failure mode is a
    # candidate who is silently unfindable by the words on their own profile.
    #
    # `simple` rather than `english` (SRS §10.5): this market writes mixed-script
    # and transliterated content, and English stemming applied to a Nepali
    # headline produces stems that match nothing. Typo tolerance comes from
    # trigram similarity beside this, not from a stemmer.
    search_vector = models.GeneratedField(
        expression=SearchVector("headline", "summary", config="simple"),
        output_field=SearchVectorField(),
        db_persist=True,
    )

    objects = CandidateProfileQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(
                fields=["visibility"],
                condition=models.Q(is_searchable=True),
                name="candprofile_searchable_idx",
            ),
            # Partial on searchable, for the same reason the job index is
            # partial on published: an invisible profile is never a candidate
            # for a vector query (ADR-A24, INV-4).
            GinIndex(fields=["search_vector"], name="candprofile_search_idx"),
            HnswIndex(
                name="candprofile_embedding_idx",
                fields=["embedding"],
                condition=models.Q(is_searchable=True),
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(salary_min__isnull=True)
                    | models.Q(salary_max__isnull=True)
                    | models.Q(salary_max__gte=models.F("salary_min"))
                ),
                name="candprofile_salary_range_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"Profile of {self.user_id}"

    @property
    def is_public(self) -> bool:
        return self.visibility == Visibility.EVERYONE


class ProfileSection(TimeStampedModel):
    """Shared shape for the ordered lists hanging off a profile.

    The `profile` foreign key is declared per model rather than here, so each
    section gets a related name a human would choose (`profile.experiences`,
    not `profile.profileexperiences`).
    """

    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        abstract = True
        ordering = ("order", "-created_at")


class ProfileExperience(ProfileSection):
    profile = models.ForeignKey(
        CandidateProfile, on_delete=models.CASCADE, related_name="experiences"
    )
    title = models.CharField(max_length=160)
    org_name = models.CharField(max_length=160)
    company = models.ForeignKey(
        "companies.Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    location = models.ForeignKey(
        "taxonomy.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=False)
    summary = models.TextField(blank=True)
    bullets = models.JSONField(default=list, blank=True)

    class Meta(ProfileSection.Meta):
        ordering = ("-is_current", "-start_date")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="experience_dates_ordered",
            ),
            # A job you still hold has no end date. Allowing both makes "current"
            # meaningless and every duration calculation ambiguous.
            models.CheckConstraint(
                condition=models.Q(is_current=False) | models.Q(end_date__isnull=True),
                name="experience_current_has_no_end",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} at {self.org_name}"


class ProfileEducation(ProfileSection):
    profile = models.ForeignKey(
        CandidateProfile, on_delete=models.CASCADE, related_name="education"
    )
    institution = models.CharField(max_length=160)
    qualification = models.CharField(max_length=160, blank=True)
    field_of_study = models.CharField(max_length=160, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=False)
    summary = models.TextField(blank=True)

    class Meta:
        # Not inheriting ProfileSection.Meta: education orders by three keys
        # rather than the shared two, and `abstract` resets to False anyway.
        ordering = ("-is_current", "-end_date", "-start_date")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(start_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="education_dates_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.qualification} — {self.institution}"


class ProfileProject(ProfileSection):
    profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=160)
    url = models.URLField(blank=True)
    summary = models.TextField(blank=True)
    started_on = models.DateField(null=True, blank=True)
    finished_on = models.DateField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class ProfileCertification(ProfileSection):
    profile = models.ForeignKey(
        CandidateProfile, on_delete=models.CASCADE, related_name="certifications"
    )
    name = models.CharField(max_length=160)
    issuer = models.CharField(max_length=160, blank=True)
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    credential_id = models.CharField(max_length=120, blank=True)
    credential_url = models.URLField(blank=True)
    file = models.ForeignKey(
        "files.Attachment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(ProfileSection.Meta):
        ordering = ("-issued_on", "order")

    def __str__(self) -> str:
        return self.name


class ProfileLanguage(ProfileSection):
    profile = models.ForeignKey(
        CandidateProfile, on_delete=models.CASCADE, related_name="languages"
    )
    language = models.ForeignKey("taxonomy.Language", on_delete=models.PROTECT, related_name="+")
    proficiency = models.CharField(max_length=8, choices=Proficiency.choices)

    class Meta(ProfileSection.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "language"], name="profilelanguage_once_per_language"
            )
        ]

    def __str__(self) -> str:
        return f"{self.language_id} ({self.proficiency})"


class ProfileLink(ProfileSection):
    profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="links")
    kind = models.CharField(max_length=16, choices=LinkKind.choices, default=LinkKind.OTHER)
    label = models.CharField(max_length=80, blank=True)
    url = models.URLField()
    status = models.CharField(
        max_length=16, choices=LinkStatus.choices, default=LinkStatus.UNCHECKED
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta(ProfileSection.Meta):
        constraints = [
            models.UniqueConstraint(fields=["profile", "url"], name="profilelink_unique_url")
        ]

    def __str__(self) -> str:
        return self.url


class ProfileAward(ProfileSection):
    profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="awards")
    name = models.CharField(max_length=160)
    issuer = models.CharField(max_length=160, blank=True)
    awarded_on = models.DateField(null=True, blank=True)
    summary = models.TextField(blank=True)

    def __str__(self) -> str:
        return self.name


class CandidateSkill(TimeStampedModel):
    """A claim that this candidate has this skill. The matcher's input.

    `source` and `evidence_ref` exist because the three ways a skill lands here
    are not equally trustworthy — self-reported, extracted from a resume, or
    inferred — and a scorer that cannot tell them apart cannot explain itself.
    """

    profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="skills")
    skill = models.ForeignKey("taxonomy.Skill", on_delete=models.PROTECT, related_name="+")
    years = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    level = models.PositiveSmallIntegerField(choices=SkillLevel.choices, null=True, blank=True)
    source = models.CharField(max_length=16, choices=SkillSource.choices, default=SkillSource.SELF)
    evidence_ref = models.JSONField(default=dict, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("order", "skill__name")
        constraints = [
            models.UniqueConstraint(fields=["profile", "skill"], name="candidateskill_once"),
            models.CheckConstraint(
                condition=models.Q(years__isnull=True) | models.Q(years__gte=0),
                name="candidateskill_years_not_negative",
            ),
        ]
        indexes = [models.Index(fields=["skill"], name="candidateskill_skill_idx")]

    def __str__(self) -> str:
        return f"{self.profile_id}: {self.skill_id}"


class BlockedCompany(TimeStampedModel):
    """The "my current employer must not see this" case.

    Hides the candidate from the company and the company's jobs from the
    candidate. Every serious job platform needs it, and a candidate who
    discovers it is missing has already been harmed.
    """

    profile = models.ForeignKey(
        CandidateProfile, on_delete=models.CASCADE, related_name="blocked_companies"
    )
    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="blocked_by"
    )
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name_plural = "blocked companies"
        constraints = [
            models.UniqueConstraint(fields=["profile", "company"], name="blockedcompany_once")
        ]

    def __str__(self) -> str:
        return f"{self.profile_id} blocks {self.company_id}"


class SavedJob(TimeStampedModel):
    """A job a candidate wants to come back to (SRS §5.4).

    Deferred from P3 because `Job` did not exist yet; it does now.
    """

    profile = models.ForeignKey(
        CandidateProfile, on_delete=models.CASCADE, related_name="saved_jobs"
    )
    job = models.ForeignKey("jobs.Job", on_delete=models.CASCADE, related_name="saved_by")
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["profile", "job"], name="savedjob_once_per_job")
        ]

    def __str__(self) -> str:
        return f"{self.profile_id} saved {self.job_id}"


class AlertFrequency(models.TextChoices):
    OFF = "off", _("No alerts")
    DAILY = "daily", _("Daily")
    WEEKLY = "weekly", _("Weekly")


class SavedSearch(TimeStampedModel):
    """A search a candidate keeps, and optionally is alerted about.

    `query` holds the filters rather than a URL: a query string is a
    presentation detail that changes when the search screen does, and an alert
    that stopped matching because a parameter was renamed is an alert nobody
    can debug.
    """

    profile = models.ForeignKey(
        CandidateProfile, on_delete=models.CASCADE, related_name="saved_searches"
    )
    name = models.CharField(max_length=120)
    query = models.JSONField(default=dict, blank=True)
    alert_frequency = models.CharField(
        max_length=8, choices=AlertFrequency.choices, default=AlertFrequency.OFF
    )
    last_alerted_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["profile", "name"], name="savedsearch_unique_name")
        ]
        indexes = [
            models.Index(
                fields=["alert_frequency"],
                condition=~models.Q(alert_frequency="off"),
                name="savedsearch_alerting_idx",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def alerts_on(self) -> bool:
        return self.alert_frequency != AlertFrequency.OFF


class ViewSource(models.TextChoices):
    """Where an employer was when they looked at a candidate.

    Recorded because the candidate sees this, and "they found me in a search"
    and "they opened my application" are different facts about the same person
    reading the same profile.
    """

    SEARCH = "search", _("From a talent search")
    APPLICATION = "application", _("From an application")
    POOL = "pool", _("From a talent pool")


class ProfileView(models.Model):
    """One employer read of one candidate's profile (FR-2.8, FR-9.3).

    **Its own table, not `AttachmentAccess`.** SRS §10.6 said a profile view
    writes an `AttachmentAccess` row; a profile is not an attachment, and a
    candidate with no CV file uploaded would have had no record written at all —
    which is the one case where "who viewed me" matters most, because a profile
    found in a search is exactly a profile nobody has a file for yet. The two
    records answer different questions and both are shown to the candidate:
    this one for "who read my profile", `AttachmentAccess` for "who downloaded
    my CV".

    Append-only in intent and in practice: nothing updates a view, and a
    company must not be able to make one disappear. The model is not
    `AppendOnlyModel` only because the retention sweep in P19 will delete old
    rows in bulk, and that guard would refuse it.

    Partition-ready per ADR-A40 — no inbound foreign keys, `created_at` in the
    index — because this is the table that grows with employer activity rather
    than with the number of candidates.
    """

    candidate = models.ForeignKey(
        CandidateProfile, on_delete=models.CASCADE, related_name="profile_views"
    )
    company = models.ForeignKey(
        "companies.Company", on_delete=models.CASCADE, related_name="candidate_views"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Who looked. Null once the account is gone; the company remains.",
    )
    source = models.CharField(max_length=16, choices=ViewSource.choices, default=ViewSource.SEARCH)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            # "Who viewed me", newest first — the candidate's screen.
            models.Index(fields=["candidate", "-created_at"], name="profileview_candidate_idx"),
            # "How much is this company looking at" — the anomaly check.
            models.Index(fields=["company", "-created_at"], name="profileview_company_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.company_id} viewed {self.candidate_id}"
