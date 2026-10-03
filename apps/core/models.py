"""
Feature flags (SRS §5.14).

`apps/core/features.py` holds the code defaults — the honest statement of what
is built. This table layers runtime overrides on top, so a module can be turned
on for staff, rolled out to a percentage, or switched off during an incident
without a deploy.

`expires_on` is mandatory. A flag that outlives its feature becomes a permanent
dead branch that nobody dares delete, and the only reliable defence is making
every flag state, at creation, when it should be gone.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel


class FeatureFlagQuerySet(models.QuerySet):
    def active(self) -> FeatureFlagQuerySet:
        return self.filter(expires_on__gte=timezone.localdate())

    def expired(self) -> FeatureFlagQuerySet:
        return self.filter(expires_on__lt=timezone.localdate())


class FeatureFlag(TimeStampedModel):
    key = models.SlugField(
        max_length=100,
        unique=True,
        help_text="Matches a key in apps/core/features.py DEFAULT_FEATURES.",
    )
    is_enabled = models.BooleanField(default=False)
    rollout_percent = models.PositiveSmallIntegerField(
        default=100,
        help_text="Share of users the flag applies to when enabled. 100 means everyone.",
    )
    staff_only = models.BooleanField(
        default=False, help_text="Enable for platform staff regardless of rollout."
    )
    expires_on = models.DateField(
        help_text="The date by which this flag must be removed or renewed."
    )
    notes = models.CharField(max_length=255, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = FeatureFlagQuerySet.as_manager()

    class Meta:
        ordering = ("key",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rollout_percent__gte=0, rollout_percent__lte=100),
                name="featureflag_rollout_percent_range",
            )
        ]

    def __str__(self) -> str:
        return f"{self.key}={'on' if self.is_enabled else 'off'}"

    def clean(self) -> None:
        if self.expires_on and self.expires_on < timezone.localdate():
            raise ValidationError({"expires_on": "Set a date in the future."})

    @property
    def is_expired(self) -> bool:
        return self.expires_on < timezone.localdate()


class LegalDocument(TimeStampedModel):
    """One version of one legal document.

    Immutable in practice rather than by trigger: a new version is a new row,
    and nothing in the product edits one. That is enough here — unlike a
    `ResumeVersion`, nobody's evidence depends on this row being unchanged, and
    a trigger would be ceremony without a threat model behind it.
    """

    class Slug(models.TextChoices):
        TERMS = "terms", _("Terms of use")
        PRIVACY = "privacy", _("Privacy policy")
        COOKIES = "cookies", _("Cookie notice")

    slug = models.CharField(max_length=20, choices=Slug.choices)
    version = models.CharField(max_length=20)
    title = models.CharField(max_length=120)
    body = models.TextField(help_text="Markdown-ish HTML, rendered inside the page shell.")
    summary = models.CharField(
        max_length=300, blank=True, help_text="What changed since the previous version."
    )
    effective_from = models.DateField(default=timezone.localdate)
    #: True while the text has not been through legal review. The page says so,
    #: in a banner, rather than the absence of review being something only the
    #: repository knows.
    is_draft = models.BooleanField(default=True)

    class Meta:
        ordering = ("slug", "-effective_from", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=["slug", "version"], name="legaldocument_one_per_version"
            )
        ]
        indexes = [
            models.Index(fields=["slug", "-effective_from"], name="legaldocument_current_idx")
        ]

    def __str__(self) -> str:
        return f"{self.slug} v{self.version}"
