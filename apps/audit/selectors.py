"""Audit reads."""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.db import models

from .models import AuditLog


def for_target(target: models.Model) -> models.QuerySet[AuditLog]:
    """Everything ever recorded against one object, newest first."""
    content_type = ContentType.objects.get_for_model(target, for_concrete_model=False)
    return (
        AuditLog.objects.filter(target_ct=content_type, target_id=str(target.pk))
        .select_related("actor", "on_behalf_of")
        .order_by("-created_at")
    )


def for_actor(user) -> models.QuerySet[AuditLog]:
    """Everything one user did, newest first."""
    return AuditLog.objects.filter(actor=user).select_related("target_ct").order_by("-created_at")


def by_verbs(verbs, *, since=None, limit: int = 200) -> models.QuerySet[AuditLog]:
    """Recent entries for a named set of verbs (SRS §15.4's anomaly queue).

    Verbs are passed in rather than matched by prefix here: a new verb should
    appear on a staff queue because somebody decided it should, not because it
    happened to start with the right word.
    """
    rows = AuditLog.objects.filter(verb__in=list(verbs))
    if since is not None:
        rows = rows.filter(created_at__gte=since)
    return rows.select_related("actor", "company").order_by("-created_at")[:limit]
