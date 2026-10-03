"""
Candidate factories.

Plain functions, matching `apps/accounts/tests/factories.py`. Rows are created
directly, which is not a service bypass: a factory does not claim to be a
domain operation, and a test that has to walk five services to reach its
starting state is testing the setup rather than the behaviour
(DEVELOPER.md §8.2).
"""

from __future__ import annotations

from apps.accounts.tests.factories import create_user
from apps.candidates.models import CandidateProfile


def create_profile(*, user=None, **overrides) -> CandidateProfile:
    return CandidateProfile.objects.create(user=user or create_user(), **overrides)
