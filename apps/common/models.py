"""
Base models, plus the one table `common` owns.

Abstract bases belong here because every domain uses them. `ConsumedToken` is
the single exception to "common owns no tables": it is infrastructure with no
domain meaning, used by files today and by password reset, invitations and
interview joins later, and giving it to any one domain app would make the other
three import across a boundary for a security primitive.
"""

from __future__ import annotations

from typing import Any

from django.db import models
from pgvector.django import VectorField

from .uuid7 import uuid7

#: Vector width, fixed for the whole platform (SRS §5.3, §5.4, §5.7).
#:
#: 768 is what the small hosted embedding models produce and what a local
#: `nomic-embed-text` produces, so development and production agree without a
#: second column. Changing it is not a settings change: it is a new column, a
#: backfill and a swap, which is exactly the procedure ADR-A27 exists to make
#: possible — and exactly why the number is a constant here rather than a knob
#: somebody can turn.
EMBEDDING_DIMENSIONS = 768


class TimeStampedModel(models.Model):
    """created_at / updated_at, set by the database clock's caller.

    `auto_now_add` and `auto_now` are used deliberately rather than defaults so
    that a bulk update cannot silently leave updated_at stale — anything that
    bypasses save() also bypasses the timestamp, and that is visible in review.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PublicIdModel(models.Model):
    """A UUIDv7 for anything that appears in a URL, an email or a file path.

    The integer primary key stays internal. Exposing it would leak how many
    rows exist, which is commercially sensitive on both sides of a two-sided
    marketplace (SRS §5).
    """

    public_id = models.UUIDField(default=uuid7, editable=False, unique=True)

    class Meta:
        abstract = True


class EmbeddedModel(models.Model):
    """A row carrying a vector, the model that produced it, and when (ADR-A27).

    The three columns travel together, always, because cosine distance between
    vectors from two different models is noise and **nothing errors** — matching
    just quietly returns nonsense. `embedding_model` is what makes retrieval
    able to filter to one model's vectors, and what makes a model change a
    procedure (add, dual-write, backfill, verify, swap) rather than an incident.

    A mixin rather than three copied columns: profiles and jobs both need them,
    talent search will need them on more, and three columns copied by hand are
    three columns that eventually disagree about their nullability.

    `embedding` is null until something embeds the row. Null is the honest state
    and every caller handles it: an unembedded job is still fully matchable,
    because the score is deterministic and vectors only affect retrieval order
    (ADR-A6, ADR-A24).
    """

    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    embedding_model = models.CharField(max_length=120, blank=True)
    embedded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def is_embedded(self) -> bool:
        return self.embedding is not None and bool(self.embedding_model)


class AppendOnlyModel(models.Model):
    """A row that may be inserted and never changed.

    The database is the real enforcement for the audit log (a role granted only
    INSERT and SELECT, scheduled with deployment hardening in P18). This guard
    is the layer above it: it turns a programming mistake into an immediate,
    obvious failure instead of a silent rewrite of history.
    """

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk is not None and not self._state.adding:
            raise ValueError(f"{type(self).__name__} is append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValueError(f"{type(self).__name__} is append-only and cannot be deleted.")


class ConsumedToken(models.Model):
    """A single-use token that has been spent (ADR-A38).

    Durable by design. See apps/common/tokens.py for why this is not a cache
    entry: the guarantee must survive eviction, `cache.clear()` and a Redis
    restart, and Redis in this deployment has persistence disabled.
    """

    jti = models.CharField(
        max_length=64,
        help_text="Token identifier from the signed payload.",
    )
    purpose = models.CharField(
        max_length=32,
        help_text="Namespace, so a token minted for one purpose cannot burn another.",
    )
    consumed_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        help_text="When this record may be purged. The signature enforces expiry independently."
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["jti", "purpose"], name="consumedtoken_single_use")
        ]
        indexes = [
            models.Index(fields=["expires_at"], name="consumedtoken_expiry_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.purpose}:{self.jti}"
