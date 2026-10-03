"""
Audit log (SRS §5.2, NFR-9).

Append-only, forever. This is the table that answers "who did that, when, on
whose behalf" for a regulator, a customer dispute or an incident review.

Three design points worth stating, because each has a failure it prevents:

  * `target_repr` is denormalised at write time. A generic foreign key cannot
    be joined cheaply, and the referenced row may be gone by the time anyone
    reads the log. Storing a human-readable label keeps the entry meaningful
    after its subject is deleted.

  * `on_behalf_of` records impersonation. Every action taken by staff while
    impersonating carries both identities, so "the user did it" and "support
    did it as the user" are never confused.

  * The application enforces append-only in `save()`; the database will enforce
    it with a role granted only INSERT and SELECT, which is deployment work
    scheduled in P18. Two layers, because the guard is bypassable by a shell
    and the grant is not.

Company scoping (`company` FK) is added in P1 with the companies app. It is
absent here rather than stubbed, because a nullable column with no referent is
indistinguishable from missing data once real rows exist.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.common.models import AppendOnlyModel


class AuditLog(AppendOnlyModel):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
        help_text="Who performed the action. Null for system actions.",
    )
    on_behalf_of = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries_impersonated",
        help_text="Set when staff acted while impersonating this user.",
    )

    verb = models.CharField(
        max_length=100,
        help_text="Dotted past-tense action, e.g. attachment.access_granted.",
    )

    target_ct = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.SET_NULL)
    target_id = models.CharField(max_length=64, blank=True)
    target = GenericForeignKey("target_ct", "target_id")
    target_repr = models.CharField(
        max_length=255,
        blank=True,
        help_text="Human-readable label captured at write time.",
    )

    company = models.ForeignKey(
        "companies.Company",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
        help_text="The tenant this action belongs to, where there is one.",
    )

    ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "audit entry"
        verbose_name_plural = "audit entries"
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["target_ct", "target_id", "-created_at"],
                name="audit_target_idx",
            ),
            models.Index(fields=["actor", "-created_at"], name="audit_actor_idx"),
            models.Index(fields=["verb", "-created_at"], name="audit_verb_idx"),
            models.Index(fields=["company", "-created_at"], name="audit_company_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.verb} by {self.actor_id or 'system'} at {self.created_at:%Y-%m-%d %H:%M}"
