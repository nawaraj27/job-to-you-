"""
Resume ingestion (SRS §5.5.2, ADR-A32).

P4 covers everything that happens to a CV *before* the builder touches it:
it arrives, it is scanned, it is deduplicated, and — only once it belongs to
someone — it is parsed into structured data the candidate is offered.

The rule that shapes all of it is ADR-A32: **a guest upload is stored and
scanned and nothing else.** No text extraction, no analysis, no AI call. Until
an account claims it, the platform holds a file it was given and knows nothing
about its contents. That is a legal position as much as a technical one, and it
is enforced here by `parse_status` starting at `blocked` for an unclaimed
upload rather than by a comment asking people to be careful.

The second rule is that extraction **proposes** (§5.5.3). A parse never writes
to the candidate's profile. It writes `ResumeImportProposal` rows, and only an
explicit accept moves anything — because a document is evidence of what someone
wrote, not permission to rewrite their profile.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.models import PublicIdModel, TimeStampedModel


class ParseStatus(models.TextChoices):
    """Where an upload is in the ingestion pipeline.

    `BLOCKED` is the initial state of a guest upload and is not an error: it
    means "nobody has claimed this, so nothing may read it". `PENDING` is the
    initial state of an owned upload waiting on the virus scanner.
    """

    BLOCKED = "blocked", _("Not claimed (processing not permitted)")
    PENDING = "pending", _("Waiting to be parsed")
    RUNNING = "running", _("Parsing")
    PARSED = "parsed", _("Parsed")
    FAILED = "failed", _("Could not be parsed")
    UNSUPPORTED = "unsupported", _("Format cannot be parsed")


class ResumeUploadQuerySet(models.QuerySet):
    def owned(self) -> ResumeUploadQuerySet:
        return self.filter(profile__isnull=False)

    def unclaimed(self) -> ResumeUploadQuerySet:
        return self.filter(profile__isnull=True)

    def parseable(self) -> ResumeUploadQuerySet:
        """Owned, clean, and not yet parsed.

        The scan status is read from the attachment rather than mirrored here:
        two copies of one fact disagree eventually, and the copy this side
        would be the stale one.
        """
        from apps.files.constants import ScanStatus

        return self.owned().filter(
            attachment__scan_status=ScanStatus.CLEAN,
            parse_status__in=[ParseStatus.PENDING, ParseStatus.FAILED],
        )


class ResumeUpload(PublicIdModel, TimeStampedModel):
    """A CV file someone gave us.

    Separate from `Attachment`, which is storage: the same bytes uploaded by
    two people are one attachment and two uploads, because "who gave us this
    and what happened to it" is per person.
    """

    profile = models.ForeignKey(
        "candidates.CandidateProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="resume_uploads",
        help_text="Null until an account claims it (ADR-A32).",
    )
    guest_token = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text=(
            "Opaque handle held by an anonymous uploader, so they can claim the "
            "file after registering. Hashed, never a session key."
        ),
    )

    attachment = models.ForeignKey(
        "files.Attachment", on_delete=models.PROTECT, related_name="resume_uploads"
    )
    original_filename = models.CharField(max_length=255)
    mime = models.CharField(max_length=127)
    size = models.BigIntegerField()
    pages = models.PositiveSmallIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, db_index=True)

    parse_status = models.CharField(
        max_length=16, choices=ParseStatus.choices, default=ParseStatus.BLOCKED
    )
    parse_error = models.CharField(max_length=255, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)

    objects = ResumeUploadQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # The same file twice is the same upload. Partial, because two
            # guests uploading identical bytes are genuinely two uploads.
            models.UniqueConstraint(
                fields=["profile", "sha256"],
                condition=models.Q(profile__isnull=False),
                name="resumeupload_one_per_profile_per_file",
            ),
            # An unclaimed upload has a token to claim it with; a claimed one
            # has an owner. A row with neither is unreachable by anybody.
            models.CheckConstraint(
                condition=(models.Q(profile__isnull=False) | ~models.Q(guest_token="")),
                name="resumeupload_reachable",
            ),
        ]
        indexes = [
            models.Index(
                fields=["created_at"],
                condition=models.Q(profile__isnull=True),
                name="resumeupload_sweep_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.original_filename

    @property
    def is_claimed(self) -> bool:
        return self.profile_id is not None

    @property
    def may_process(self) -> bool:
        """Processing is permitted only once someone owns this (ADR-A32)."""
        return self.is_claimed


class ResumeParse(TimeStampedModel):
    """What a deterministic reader found in one upload.

    One row per upload: re-parsing replaces it, because a parse is a pure
    function of (file, engine version) and keeping every attempt would grow a
    table nobody queries with data nobody compares.

    `raw_text` is the extracted text and is personal data of the most sensitive
    kind the platform holds — DEVELOPER.md §2.13 forbids it in logs, events and
    audit metadata, and nothing here puts it there.
    """

    upload = models.OneToOneField(ResumeUpload, on_delete=models.CASCADE, related_name="parse")

    engine = models.CharField(max_length=40, help_text="Which reader produced this.")
    engine_version = models.CharField(max_length=40)

    raw_text = models.TextField(blank=True)
    structured = models.JSONField(default=dict, blank=True)
    structured_schema_version = models.PositiveSmallIntegerField(default=1)
    confidence = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Parse of {self.upload_id} by {self.engine} {self.engine_version}"

    @property
    def is_empty(self) -> bool:
        """A PDF of scanned images extracts to nothing. That is a real answer."""
        return not self.raw_text.strip()


class ProposalKind(models.TextChoices):
    SKILL = "skill", _("Skill")
    EXPERIENCE = "experience", _("Work experience")
    EDUCATION = "education", _("Education")
    LANGUAGE = "language", _("Language")
    CONTACT = "contact", _("Contact detail")
    HEADLINE = "headline", _("Headline")


class ProposalState(models.TextChoices):
    OFFERED = "offered", _("Offered")
    ACCEPTED = "accepted", _("Accepted")
    REJECTED = "rejected", _("Rejected")
    SUPERSEDED = "superseded", _("Superseded by a newer parse")


class ResumeImportProposal(TimeStampedModel):
    """Something a parse thinks belongs on the profile (§5.5.3, ADR-A49).

    Never applied automatically. A CV is evidence of what someone once wrote,
    not authority to rewrite their profile — a stale employer or an out-of-date
    phone number would silently overwrite the record the matcher reads.

    `payload` holds the proposed values in the shape the receiving service
    takes, so accepting is one service call rather than a translation layer
    that can disagree with what was displayed.
    """

    parse = models.ForeignKey(ResumeParse, on_delete=models.CASCADE, related_name="proposals")
    profile = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="import_proposals"
    )

    kind = models.CharField(max_length=16, choices=ProposalKind.choices)
    label = models.CharField(max_length=200, help_text="What to show the candidate.")
    payload = models.JSONField(default=dict)
    confidence = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    evidence = models.CharField(
        max_length=300,
        blank=True,
        help_text="The line the proposal came from, so the candidate can judge it.",
    )

    state = models.CharField(
        max_length=16, choices=ProposalState.choices, default=ProposalState.OFFERED
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    outcome = models.CharField(
        max_length=200, blank=True, help_text="What accepting it actually did, or why it failed."
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("kind", "order", "pk")
        indexes = [
            models.Index(
                fields=["profile", "state"],
                condition=models.Q(state="offered"),
                name="proposal_open_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(state="offered", decided_at__isnull=True) | ~models.Q(state="offered")
                ),
                name="proposal_offered_is_undecided",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.label}"

    @property
    def is_open(self) -> bool:
        return self.state == ProposalState.OFFERED


class BuilderMode(models.TextChoices):
    """How the candidate is writing this document.

    Three modes rather than one because they suit different people and
    different starting points, and the SRS treats them as first-class: the
    editor for someone who has a CV, the guided flow for someone starting cold,
    the assistant for someone who would rather talk than type.
    """

    EDITOR = "editor", _("Editor")
    GUIDED = "guided", _("Guided")
    ASSISTANT = "assistant", _("Assistant")


class DocumentKind(models.TextChoices):
    UPLOADED = "uploaded", _("From an uploaded CV")
    BUILT = "built", _("Built here")


class TemplateLayout(models.TextChoices):
    """The page structures a template may name.

    A closed vocabulary rather than free text, and deliberately shorter than
    the template catalogue: a structure is renderer code — a frame layout in
    the PDF, a grid in the preview — while an accent or a font is a data edit.
    Adding a tenth accent costs nothing; adding a tenth structure is a
    decision, and this list is where that decision is recorded.

    Every value here has an entry in `rendering/theme.py`, which is what the
    PDF, the DOCX and the browser preview all read.
    """

    CLASSIC = "classic", _("Classic")
    COMPACT = "compact", _("Compact")
    SIDEBAR = "sidebar", _("Sidebar")
    MODERN = "modern", _("Modern")
    EXECUTIVE = "executive", _("Executive")
    MINIMAL = "minimal", _("Minimal")
    TECHNICAL = "technical", _("Technical")
    ACADEMIC = "academic", _("Academic")
    PORTFOLIO = "portfolio", _("Portfolio")
    CREATIVE = "creative", _("Creative")


class TemplateDensity(models.TextChoices):
    """How tightly a template sets its type.

    Separate from layout because "the same template, tighter" is a real
    request — ten years of history that has to fit two pages — and does not
    deserve a duplicate structure in the renderers.
    """

    NORMAL = "normal", _("Normal")
    COMPACT = "compact", _("Compact")


class TemplateTier(models.TextChoices):
    FREE = "free", _("Free")
    PRO = "pro", _("Pro")


class ResumeTemplate(TimeStampedModel):
    """A layout a document can be rendered in (SRS 5.5.2).

    A table rather than a code constant because a template is content: staff
    add one, retire one, or mark one Pro without a deploy. `is_active` rather
    than deletion — a document already using a retired template must keep
    rendering, and every finalized version snapshots its template anyway.
    """

    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=80)
    family = models.CharField(max_length=40, blank=True)
    layout = models.CharField(
        max_length=16, choices=TemplateLayout.choices, default=TemplateLayout.CLASSIC
    )
    accent = models.CharField(max_length=20, blank=True)
    font = models.CharField(max_length=40, blank=True)
    density = models.CharField(
        max_length=8, choices=TemplateDensity.choices, default=TemplateDensity.NORMAL
    )
    tier = models.CharField(max_length=8, choices=TemplateTier.choices, default=TemplateTier.FREE)
    suits = models.CharField(max_length=200, blank=True, help_text="Who this template is for.")
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    preview = models.ForeignKey(
        "files.Attachment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("order", "name")
        indexes = [
            models.Index(
                fields=["order"], condition=models.Q(is_active=True), name="template_active_idx"
            )
        ]

    def __str__(self) -> str:
        return self.name

    def snapshot(self) -> dict:
        """The template as a finalized version records it.

        Denormalised into the version because a template's accent or font can
        change next month, and a version that rendered one way must not start
        rendering another (ADR-A8).
        """
        return {
            "slug": self.slug,
            "name": self.name,
            "layout": self.layout,
            "accent": self.accent,
            "font": self.font,
            "density": self.density,
            "tier": self.tier,
        }


class ResumeDocumentQuerySet(models.QuerySet):
    def alive(self) -> ResumeDocumentQuerySet:
        return self.filter(deleted_at__isnull=True)

    def active(self) -> ResumeDocumentQuerySet:
        return self.alive().filter(archived_at__isnull=True)


class ResumeDocument(PublicIdModel, TimeStampedModel):
    """One CV a candidate is working on (SRS 5.5.1, ADR-A19).

    The document is the identity, the draft is where writing happens, and
    versions are the historical record. Keeping the three apart is what lets
    autosave be cheap and a version mean something.
    """

    profile = models.ForeignKey(
        "candidates.CandidateProfile", on_delete=models.CASCADE, related_name="resume_documents"
    )
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=DocumentKind.choices, default=DocumentKind.BUILT)
    upload = models.ForeignKey(
        ResumeUpload, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )
    template = models.ForeignKey(ResumeTemplate, on_delete=models.PROTECT, related_name="documents")
    current_version = models.ForeignKey(
        "ResumeVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: The newest background improvement job on this CV, if there has ever
    #: been one. A pointer rather than a lookup because the builder screens sit
    #: under NFR-2's query ceiling: read with the document row it costs nothing,
    #: and on a CV that has never had a job it is simply empty.
    latest_improvement_job = models.ForeignKey(
        "ImprovementJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    builder_mode = models.CharField(
        max_length=16, choices=BuilderMode.choices, default=BuilderMode.EDITOR
    )
    is_primary = models.BooleanField(default=False)
    #: When the candidate first kept this CV: any of the four acts that freeze
    #: a version (saved, analysed, applied, exported). Null means an unsaved
    #: draft, which is listed apart from saved CVs and expires if abandoned
    #: (`services.documents.purge_abandoned_drafts`, docs/CV-Lifecycle.md).
    saved_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ResumeDocumentQuerySet.as_manager()

    class Meta:
        ordering = ("-updated_at",)
        constraints = [
            # Exactly one primary CV, enforced by the database rather than by a
            # service that races itself across two tabs (SRS 5.5.2).
            models.UniqueConstraint(
                fields=["profile"],
                condition=models.Q(is_primary=True, deleted_at__isnull=True),
                name="resumedocument_one_primary_per_profile",
            ),
        ]
        indexes = [
            models.Index(
                fields=["profile", "-updated_at"],
                condition=models.Q(deleted_at__isnull=True),
                name="resumedocument_live_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_saved(self) -> bool:
        return self.saved_at is not None


class ResumeDraft(TimeStampedModel):
    """The mutable writing surface (ADR-A19).

    Autosave writes here and never creates a version. A twenty-minute session
    that produced hundreds of immutable rows would make the version list
    unusable and "restore" meaningless, which is the whole reason these are two
    tables.
    """

    document = models.OneToOneField(ResumeDocument, on_delete=models.CASCADE, related_name="draft")
    content = models.JSONField(default=dict)
    content_schema_version = models.PositiveSmallIntegerField(default=1)
    row_version = models.PositiveIntegerField(
        default=0, help_text="Optimistic lock. Two tabs on one CV is the normal case."
    )
    dirty = models.BooleanField(
        default=False, help_text="True when the draft has moved on from the current version."
    )
    saved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Draft of {self.document_id}"


class ResumeDraftSnapshot(models.Model):
    """One step of undo history — a bounded ring, not an archive.

    Twenty-five entries, trimmed on insert: deep enough to recover from a
    mistaken bulk action, shallow enough that a long session does not grow
    without bound. Replaces the session-backed undo stack the frontend shipped
    with (SRS 5.5.2).
    """

    draft = models.ForeignKey(ResumeDraft, on_delete=models.CASCADE, related_name="snapshots")
    seq = models.PositiveIntegerField()
    content = models.JSONField()
    label = models.CharField(max_length=80, blank=True, help_text="What produced this state.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-seq",)
        constraints = [
            models.UniqueConstraint(fields=["draft", "seq"], name="draftsnapshot_unique_seq")
        ]

    def __str__(self) -> str:
        return f"{self.draft_id}#{self.seq}"


class EnrichmentState(models.TextChoices):
    """Where the AI half of a report has got to.

    The deterministic half — the score, the checks, the measurable findings —
    is milliseconds and is the whole of the number. The model's half is the
    verdict and the judgement findings, and on a modest model it is a minute
    and a half. Running both before answering meant a candidate pressed
    "Analyse" and watched a spinner for ninety seconds.

    So the report is written and returned without it, and this records what
    happened to the rest.
    """

    SKIPPED = "skipped", _("Not asked for")
    PENDING = "pending", _("Being read")
    DONE = "done", _("Read")
    FAILED = "failed", _("Could not be read")


class VersionReason(models.TextChoices):
    """The only four acts that may create a version (SRS 5.5.1).

    Autosave is deliberately absent. Adding a fifth member is an architecture
    change, not a convenience.
    """

    SAVED = "saved", _("Saved by the candidate")
    ANALYSED = "analysed", _("Sent for analysis")
    APPLIED = "applied", _("Attached to an application")
    EXPORTED = "exported", _("Exported")


class ResumeVersion(models.Model):
    """An immutable snapshot of a document (ADR-A8, ADR-A38).

    Immutability is enforced three ways because one is never enough:
    `finalized_at` is NOT NULL and set on insert, `save()` refuses to update a
    finalized row, and a Postgres BEFORE UPDATE trigger rejects the write
    whatever issued it — including a data-fix script.

    The trigger permits exactly one transition, `redacted_at IS NULL` to
    `NOT NULL`, because enforcing immutability literally made privacy erasure
    impossible: the row holds contact details, could not be updated, and could
    not be deleted while an application referenced it (ADR-A38).

    `sha256` survives redaction deliberately. It proves that a specific
    document was submitted and when, while the platform holds none of it.
    """

    document = models.ForeignKey(ResumeDocument, on_delete=models.CASCADE, related_name="versions")
    version_no = models.PositiveIntegerField()
    content = models.JSONField()
    content_schema_version = models.PositiveSmallIntegerField(default=1)
    template_snapshot = models.JSONField(default=dict)
    sha256 = models.CharField(max_length=64, db_index=True)

    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_reason = models.CharField(max_length=16, choices=VersionReason.choices)
    change_note = models.CharField(max_length=200, blank=True)
    finalized_at = models.DateTimeField()

    redacted_at = models.DateTimeField(null=True, blank=True)
    redaction_reason = models.CharField(max_length=120, blank=True)
    redacted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-version_no",)
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version_no"], name="resumeversion_unique_number"
            ),
        ]
        indexes = [
            models.Index(fields=["document", "-finalized_at"], name="resumeversion_history_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.document_id} v{self.version_no}"

    def save(self, *args, **kwargs):
        """Refuse any update that is not the one permitted redaction.

        The layer above the trigger: it turns a programming mistake into an
        immediate, readable failure instead of a database error nobody can act
        on.
        """
        if self.pk is not None:
            update_fields = set(kwargs.get("update_fields") or ())
            permitted = {"redacted_at", "redaction_reason", "redacted_by", "content"}
            if not update_fields or not update_fields.issubset(permitted):
                raise ValueError(
                    "A finalized resume version cannot be changed. Create a new version, or "
                    "redact this one through resumes.services.versions.redact()."
                )
        return super().save(*args, **kwargs)

    @property
    def is_redacted(self) -> bool:
        return self.redacted_at is not None


class SuggestionState(models.TextChoices):
    OFFERED = "offered", _("Offered")
    ACCEPTED = "accepted", _("Accepted")
    REJECTED = "rejected", _("Rejected")
    #: Replaced by a later proposal for the same thing, without the candidate
    #: deciding either way. Distinct from `REJECTED`, which is a person saying
    #: no: a candidate who corrects themselves mid-conversation — "actually it
    #: was two years and I was a teller" — was left with "Manager" and "Teller"
    #: both sitting in the list, either of which could be accepted.
    SUPERSEDED = "superseded", _("Superseded")


class ResumeSuggestion(TimeStampedModel):
    """A proposed edit to a draft (INV-3).

    The mechanism that makes "nothing writes to a document without an accept"
    structural rather than a promise: a suggestion is a row, and only an accept
    writes the draft. P5 fills this table from the reconcile protocol; P12 adds
    AI-generated wording, and the accept path is already the only way in.
    """

    draft = models.ForeignKey(ResumeDraft, on_delete=models.CASCADE, related_name="suggestions")
    target_path = models.CharField(
        max_length=200, help_text="Where in the content it applies, e.g. sections.2.entries.0"
    )
    original = models.TextField(blank=True)
    proposed = models.TextField()
    source = models.CharField(
        max_length=24,
        default="reconcile",
        help_text="What produced it: `reconcile`, `ai`, or `finding` for a fix applied from a report.",
    )
    finding = models.ForeignKey(
        "resumes.ResumeFinding",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="suggestions",
        help_text="The finding this fixes, when it came from an analysis report (FR-4.9).",
    )
    ai_run = models.ForeignKey(
        "ai.AIRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resume_suggestions",
        help_text=(
            "The run that wrote it, for AI-sourced wording (ADR-A11). Null for a "
            "reconcile suggestion, which no model touched."
        ),
    )
    rationale = models.CharField(max_length=200, blank=True)
    state = models.CharField(
        max_length=16, choices=SuggestionState.choices, default=SuggestionState.OFFERED
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("created_at",)
        indexes = [
            models.Index(
                fields=["draft"], condition=models.Q(state="offered"), name="suggestion_open_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.target_path}: {self.proposed[:40]}"

    @property
    def is_open(self) -> bool:
        return self.state == SuggestionState.OFFERED


class BuilderSession(TimeStampedModel):
    """Where a builder mode left off (SRS 5.5.2).

    One row per (document, mode): switching from guided to the editor and back
    must not lose the guided flow's position, and the two modes share nothing
    else.
    """

    document = models.ForeignKey(
        ResumeDocument, on_delete=models.CASCADE, related_name="builder_sessions"
    )
    mode = models.CharField(max_length=16, choices=BuilderMode.choices)
    step_state = models.JSONField(default=dict, blank=True)
    transcript = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document", "mode"], name="buildersession_one_per_mode")
        ]

    def __str__(self) -> str:
        return f"{self.document_id}:{self.mode}"


class ImprovementJobKind(models.TextChoices):
    #: One answer in the assistant conversation, drafted into suggestions.
    TURN = "turn", _("Answer")
    #: The safe rewordings an improvement plan lists, drafted as suggestions.
    PREPARE = "prepare", _("Prepare improvements")


class ImprovementJobState(models.TextChoices):
    QUEUED = "queued", _("Queued")
    PROCESSING = "processing", _("Processing")
    #: A model call is in flight. Its own state because it is the long part,
    #: and the one a candidate is entitled to be told about.
    WAITING_AI = "waiting_ai", _("Waiting for the AI")
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")


class ImprovementJob(PublicIdModel, TimeStampedModel):
    """One piece of CV improvement work that runs off the request path.

    The assistant used to answer inside the request that sent the answer: a
    candidate pressed Send, the button greyed out, and for the minute and a
    half a local model takes nothing on the screen said whether the answer had
    been received, whether anything was happening, or whether it had failed —
    and a refresh lost the lot. The work now runs on the `ai` queue and this
    row is what every screen reads: what was asked, which stage it has reached,
    what came of it, and whether it can be tried again.

    **At most one active job per document** (a partial unique constraint), so
    a double click, a second tab or a refresh-and-resubmit can never queue the
    same work twice; `request_token` makes a resubmitted form return the job
    it already created.
    """

    ACTIVE_STATES = (
        ImprovementJobState.QUEUED,
        ImprovementJobState.PROCESSING,
        ImprovementJobState.WAITING_AI,
    )

    document = models.ForeignKey(
        ResumeDocument, on_delete=models.CASCADE, related_name="improvement_jobs"
    )
    kind = models.CharField(max_length=12, choices=ImprovementJobKind.choices)
    state = models.CharField(
        max_length=12, choices=ImprovementJobState.choices, default=ImprovementJobState.QUEUED
    )
    stage = models.CharField(
        max_length=24, blank=True, help_text="Which step of the work it has reached."
    )
    stage_detail = models.CharField(
        max_length=120, blank=True, help_text="A count or a name for the current step."
    )
    request_token = models.CharField(
        max_length=64, blank=True, help_text="The submitting form's one-time token."
    )
    payload = models.JSONField(default=dict, blank=True, help_text="What was asked.")
    result = models.JSONField(default=dict, blank=True, help_text="What came of it.")
    attempts = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=["document"],
                condition=models.Q(state__in=["queued", "processing", "waiting_ai"]),
                name="improvementjob_one_active_per_document",
            ),
            models.UniqueConstraint(
                fields=["document", "request_token"],
                condition=~models.Q(request_token=""),
                name="improvementjob_token_once",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.state}:{self.document_id}"

    @property
    def is_active(self) -> bool:
        return self.state in self.ACTIVE_STATES

    @property
    def is_failed(self) -> bool:
        return self.state == ImprovementJobState.FAILED


class FindingSeverity(models.TextChoices):
    """How a finding is meant to be read (FR-4.4).

    Three members, and `strength` is one of them on purpose: a report that only
    lists faults is a report people stop opening, and the strengths are what
    tell a candidate which parts of the CV not to rewrite.
    """

    CRITICAL = "critical", _("Fix this")
    IMPROVE = "improve", _("Worth improving")
    STRENGTH = "strength", _("Working well")


class CheckStatus(models.TextChoices):
    PASS = "pass", _("Pass")
    WARN = "warn", _("Warning")
    FAIL = "fail", _("Fail")


class ResumeAnalysis(PublicIdModel, TimeStampedModel):
    """One report on one immutable version (FR-4.1, SRS §5.5.2).

    Analysis runs against a `ResumeVersion` rather than a draft so a score is
    always attributable to an exact document, and so score history (FR-4.8)
    compares like with like. A score against a moving draft would mean nothing
    a week later.

    **The score is deterministic (ADR-A6).** `algorithm_version` records which
    scorer produced it, `breakdown` holds every component and its note, and
    `ai_run` — when present — points at the model call that wrote the prose and
    the judgement findings. The model never moves the number. That is what makes
    the report reproducible, defensible, and still useful when AI is switched
    off: the deterministic half runs regardless (SRS §9.3.6).
    """

    version = models.ForeignKey(ResumeVersion, on_delete=models.CASCADE, related_name="analyses")
    target_role = models.CharField(
        max_length=120, blank=True, help_text="What the candidate said they are aiming at."
    )
    job = models.ForeignKey(
        "jobs.Job",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resume_analyses",
        help_text="Analysed against one posting's requirements, when the candidate asked for that.",
    )

    score = models.PositiveSmallIntegerField(default=0, help_text="0-100, deterministic.")
    breakdown = models.JSONField(
        default=dict,
        blank=True,
        help_text="Every scored component, its weight, its points and its note.",
    )
    verdict = models.TextField(blank=True, help_text="What a recruiter would conclude.")
    headline = models.CharField(max_length=200, blank=True)
    priority = models.JSONField(
        default=list, blank=True, help_text="The codes of the findings to fix first, in order."
    )
    experience = models.JSONField(default=dict, blank=True)
    education = models.JSONField(default=dict, blank=True)
    readability = models.JSONField(default=dict, blank=True)
    skill_evidence = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Which skills the CV supports with a bullet and which are list-only "
            "(FR-4.5). JSONB rather than a table: only ever read with its parent."
        ),
    )
    target = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "What this CV was measured against and how it did: the role, the "
            "skills the advert asks for, which of them the CV already answers, "
            "which it does not, and the keywords a search would miss. Empty "
            "when the candidate analysed without naming a target."
        ),
    )
    advert = models.TextField(
        blank=True,
        help_text=(
            "The job advert the candidate pasted, kept so the report can be "
            "re-read and re-run against the same text. Never sent anywhere by "
            "itself; the extraction above is deterministic."
        ),
    )

    ai_run = models.ForeignKey(
        "ai.AIRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resume_analyses",
        help_text="The run that wrote the prose. Null when AI was off or failed (ADR-A11).",
    )
    enrichment = models.CharField(
        max_length=12,
        choices=EnrichmentState.choices,
        default=EnrichmentState.SKIPPED,
        help_text=(
            "Whether the AI review has run. The deterministic report is written "
            "first and shown immediately; this says whether prose and judgement "
            "findings are still on their way, so the screen can say so rather "
            "than looking finished and then changing."
        ),
    )
    algorithm_version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=["version", "-created_at"], name="resumeanalysis_version_idx"),
        ]

    def __str__(self) -> str:
        return f"Analysis of {self.version_id}: {self.score}"

    @property
    def has_ai(self) -> bool:
        """Whether a model actually contributed (SRS §9.3.9).

        A failed run is still linked — that is the point of recording every run
        (ADR-A11) — but it wrote nothing, and a report labelled "AI-assisted"
        because a provider timed out would be a lie told by a foreign key.
        """
        return self.ai_run is not None and self.ai_run.status == "ok"

    @property
    def document(self) -> ResumeDocument:
        return self.version.document


class ResumeFinding(models.Model):
    """One thing worth saying about a CV (FR-4.4).

    `code` is stable and machine-readable; `title` and `text` are what a person
    reads. The separation is what lets a deterministic check and a model produce
    findings that render identically, and what lets score history say "this
    problem has been here for three versions".
    """

    analysis = models.ForeignKey(ResumeAnalysis, on_delete=models.CASCADE, related_name="findings")
    severity = models.CharField(max_length=12, choices=FindingSeverity.choices)
    code = models.CharField(max_length=60, help_text="Stable identifier, e.g. `no_outcomes`.")
    title = models.CharField(max_length=160)
    text = models.TextField(blank=True)
    evidence = models.TextField(
        blank=True, help_text="The words on the page this is about, capped and escaped on render."
    )
    action = models.CharField(max_length=300, blank=True, help_text="What to do about it today.")
    section_ref = models.CharField(
        max_length=200,
        blank=True,
        help_text="Where it applies, in the suggestion path vocabulary, so a fix can be offered.",
    )
    order = models.PositiveSmallIntegerField(default=0)
    from_ai = models.BooleanField(
        default=False, help_text="Labelled in the interface either way (SRS §9.3.9)."
    )

    class Meta:
        ordering = ("order", "pk")
        indexes = [
            models.Index(fields=["analysis", "severity"], name="resumefinding_severity_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.severity}: {self.title}"

    #: The two shapes a rewrite can honestly be drafted for: prose a model can
    #: write from what the CV already says. Everything else a finding points at
    #: — an email address, a date range, the skills list — is fact the
    #: candidate holds and the platform does not.
    DRAFTABLE_SUFFIXES = (".bullets",)
    DRAFTABLE_PATHS = ("summary",)
    #: Findings about something that is not on the page at all — an entry with
    #: no description. There is no wording to improve, only facts to supply, so
    #: no rewrite is offered: a model asked to "fix" an empty role can only
    #: invent one.
    NEEDS_FACTS_CODES = ("thin_entry", "thin_project")

    @property
    def is_fixable(self) -> bool:
        """Whether "Draft a fix" can be offered (FR-4.9).

        Narrower than "has somewhere to write", which is what this used to
        mean, and what made the button lie. A finding about the skills list
        carries a `section_ref` pointing at that list and an `action` reading
        "Name one of them in a bullet where you used it, or drop it." There was
        no rewrite to draft, so the suggestion fell back to the action text —
        and accepting it replaced ten real skills with one sentence of advice.
        The same fallback would have written "Add the missing contact details
        at the top of the CV." into a candidate's email field.

        So the button now appears only where a rewrite genuinely exists. The
        advice for every other finding is still on the screen; it is guidance,
        which is what it always was, and it is no longer one click from being
        pasted into somebody's CV.
        """
        if self.severity == FindingSeverity.STRENGTH:
            return False
        if self.code in self.NEEDS_FACTS_CODES:
            return False
        ref = self.section_ref or ""
        return ref in self.DRAFTABLE_PATHS or ref.endswith(self.DRAFTABLE_SUFFIXES)


class ResumeAtsCheck(models.Model):
    """One deterministic machine-readability check (FR-4.2).

    No model is involved in any of these, ever. An applicant tracking system is
    a parser, and whether a parser can read a document is a fact about the
    document — asking a language model to guess at it would produce a plausible
    answer with no relationship to what the parser does.
    """

    analysis = models.ForeignKey(ResumeAnalysis, on_delete=models.CASCADE, related_name="checks")
    key = models.CharField(max_length=60)
    label = models.CharField(max_length=160)
    status = models.CharField(max_length=8, choices=CheckStatus.choices)
    note = models.CharField(max_length=300, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("order", "pk")
        constraints = [
            models.UniqueConstraint(fields=["analysis", "key"], name="resumeatscheck_one_per_key")
        ]

    def __str__(self) -> str:
        return f"{self.key}: {self.status}"


class RenderFormat(models.TextChoices):
    PDF = "pdf", _("PDF")
    DOCX = "docx", _("Word (.docx)")


class ResumeRender(TimeStampedModel):
    """One rendered file (SRS 5.5.2).

    Rendering is a pure function of **(version, template, format)**: the
    version is immutable, the template is snapshotted onto it, and the engine
    is deterministic. So the unique constraint on those three columns *is* the
    cache — a second export of the same document returns the same file rather
    than burning CPU on a byte-identical result.

    `sha256` is the digest of the rendered bytes, not of the content. It is
    what ADR-A8 means by "the SHA-256 of the rendered file submitted": an
    employer's copy can be proven identical to what the candidate sent.
    """

    version = models.ForeignKey(ResumeVersion, on_delete=models.CASCADE, related_name="renders")
    template = models.ForeignKey(ResumeTemplate, on_delete=models.PROTECT, related_name="renders")
    format = models.CharField(max_length=8, choices=RenderFormat.choices)

    attachment = models.ForeignKey(
        "files.Attachment", on_delete=models.PROTECT, related_name="resume_renders"
    )
    sha256 = models.CharField(max_length=64, db_index=True)
    engine = models.CharField(max_length=40, help_text="Which renderer produced this.")
    engine_version = models.CharField(max_length=40)
    pages = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    fonts = models.JSONField(
        default=dict,
        blank=True,
        help_text="Which fonts were actually used, so a layout change is explicable.",
    )

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["version", "template", "format"], name="resumerender_one_per_combination"
            )
        ]
        indexes = [
            models.Index(fields=["version", "format"], name="resumerender_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.version_id} as {self.format}"

    @property
    def filename(self) -> str:
        """What the browser saves it as."""
        stem = self.version.document.name or "cv"
        safe = "".join(
            character if character.isalnum() or character in " -_" else "" for character in stem
        ).strip()
        return f"{safe or 'cv'} v{self.version.version_no}.{self.format}"
