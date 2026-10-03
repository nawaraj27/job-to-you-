"""
Legal documents (SRS §5.14, §15.5, ADR-A33).

A version of the terms is a **row**, not a template. Three reasons, and the
third is the one that made this a P19 item rather than a nicety:

  * `Consent.policy_version` has to mean something. Recording that somebody
    agreed to "1.2" is only useful if 1.2 can be produced later, exactly as
    they saw it.
  * A change to the terms is a business event with a date, and a template edit
    has neither. `effective_from` is what makes "agreed under the old terms"
    a query rather than an argument.
  * The pages said *"Last updated {% now "F Y" %}"* — which rendered today's
    date every time, whatever the text said. A document that dates itself by
    the clock is telling the reader something false on every page load.

**Acceptance is a `Consent` row, not a second table** (ADR-A33). One shape,
one place a regulator's question is answered from.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from .models import LegalDocument

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User


def current(slug: str, *, on: dt.date | None = None) -> LegalDocument | None:
    """The version in force. `None` when none has been published yet.

    `None` rather than an exception: a marketing page must render before
    anybody has seeded a document, and a legal page that 500s is worse than one
    that says it is not published.
    """
    return (
        LegalDocument.objects.filter(slug=slug, effective_from__lte=on or timezone.localdate())
        .order_by("-effective_from", "-version")
        .first()
    )


def version_of(slug: str) -> str:
    """What to record on a `Consent` row. Empty when nothing is published."""
    document = current(slug)
    return document.version if document is not None else ""


@transaction.atomic
def publish(
    *,
    actor: User | None,
    slug: str,
    version: str,
    title: str,
    body: str,
    summary: str = "",
    effective_from: dt.date | None = None,
    is_draft: bool = True,
) -> LegalDocument:
    """Add a version. Never edits one.

    Editing a published version in place would rewrite what somebody already
    agreed to, and `Consent.policy_version` would then point at text that no
    longer exists.
    """
    from apps.audit import services as audit
    from apps.common.errors import Conflict

    if LegalDocument.objects.filter(slug=slug, version=version).exists():
        raise Conflict(f"{slug} v{version} already exists. Publish a new version instead.")

    document = LegalDocument.objects.create(
        slug=slug,
        version=version,
        title=title,
        body=body,
        summary=summary,
        effective_from=effective_from or timezone.localdate(),
        is_draft=is_draft,
    )
    audit.record(
        verb="legal.document_published",
        actor=actor,
        target=document,
        metadata={"slug": slug, "version": version, "draft": is_draft},
    )
    return document


def outstanding_for(user: User) -> list[LegalDocument]:
    """Documents in force that this person has not accepted the current version of.

    Only the two that require acceptance. A cookie notice is information, not
    an agreement, and asking somebody to accept one is the pattern that taught
    everybody to click through consent screens without reading them.
    """
    from apps.accounts.models import Consent

    required = {
        LegalDocument.Slug.TERMS: Consent.Purpose.TERMS,
        LegalDocument.Slug.PRIVACY: Consent.Purpose.PRIVACY,
    }

    outstanding = []
    for slug, purpose in required.items():
        document = current(slug)
        if document is None:
            continue
        accepted = Consent.objects.filter(
            user=user, purpose=purpose, policy_version=document.version, granted=True
        ).exists()
        if not accepted:
            outstanding.append(document)
    return outstanding
