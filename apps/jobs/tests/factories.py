"""
Job factories.

Plain functions, per DEVELOPER.md §8.2. Rows are created directly — which is
not a service bypass, because a factory does not claim to be a domain
operation — but `create_job` deliberately produces a *publishable* draft, so a
test that means to exercise the publish guard has to break something itself
rather than rely on an incomplete fixture.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

from django.utils import timezone

from apps.jobs.models import Job, JobStatus, Workplace
from apps.taxonomy.models import Skill

_counter = itertools.count(1)


def create_job(company, **overrides) -> Job:
    index = next(_counter)
    fields = {
        "company": company,
        "slug": f"{company.slug}-role-{index}",
        "title": f"Backend Engineer {index}",
        "description": "We are hiring a backend engineer to own the billing service.",
        "workplace": Workplace.ON_SITE,
        "salary_min": Decimal("80000"),
        "salary_max": Decimal("120000"),
        "currency": "NPR",
        "period": "month",
    }
    fields.update(overrides)
    return Job.objects.create(**fields)


def publish(job: Job) -> Job:
    """Put a job into the published state without going through the service.

    Used only where publication is the *setup*, never where it is the thing
    under test.
    """
    job.status = JobStatus.PUBLISHED
    job.published_at = job.published_at or timezone.now()
    job.save(update_fields=["status", "published_at"])
    return job


def create_skill(name: str = "Python", **overrides) -> Skill:
    fields = {"name": name, "slug": name.lower().replace(" ", "-")}
    fields.update(overrides)
    return Skill.objects.create(**fields)
