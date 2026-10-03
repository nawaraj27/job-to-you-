"""
Application factories.

Plain functions, per DEVELOPER.md §8.2. `submit` goes through the real service
rather than creating a row, because almost everything an application test cares
about — the frozen CV, the snapshot, the first timeline entry, the visibility
grant — is created by that service and not by the model.
"""

from __future__ import annotations

from django.core.management import call_command

from apps.applications.models import Application
from apps.applications.services import apply as apply_service
from apps.candidates.tests.factories import create_profile
from apps.resumes.models import VersionReason
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import versions as version_service


def seed_templates() -> None:
    """A document needs a template to exist at all."""
    call_command("seed_resume_templates", quiet_summary=True)
    call_command("seed_plans", quiet_summary=True)


def create_version(profile=None, *, name: str = "Backend CV"):
    """A finalized, sendable CV version."""
    from apps.resumes.tests.test_rendering import sample_content

    profile = profile or create_profile()
    document = document_service.create_document(profile=profile, actor=profile.user, name=name)
    draft_service.autosave(draft=document.draft, content=sample_content())
    document.refresh_from_db()
    return version_service.finalize(
        document=document, actor=profile.user, reason=VersionReason.SAVED
    )


def submit(*, job, profile=None, **kwargs) -> Application:
    """One application, through the service that makes it real."""
    profile = profile or create_profile()
    version = kwargs.pop("resume_version", None) or create_version(profile)
    return apply_service.apply_to_job(
        job=job,
        profile=profile,
        actor=profile.user,
        resume_version=version,
        **kwargs,
    )
