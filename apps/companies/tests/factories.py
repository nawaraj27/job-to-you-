"""Company factories.

Plain functions, per DEVELOPER.md §8.2. They create rows directly, which is not
a service bypass — they do not claim to be domain operations — but note that
`create_company` here deliberately does *not* create an owner membership, so a
test can construct the exact membership shape it means to exercise.
"""

from __future__ import annotations

import itertools

from apps.companies.models import Company, CompanyMembership, CompanyVerification
from apps.companies.permissions import Role

_counter = itertools.count(1)


def create_company(**overrides) -> Company:
    index = next(_counter)
    fields = {"slug": f"acme-{index}", "name": f"Acme {index}"}
    fields.update(overrides)
    company = Company.objects.create(**fields)
    CompanyVerification.objects.create(company=company)
    return company


def add_member(company: Company, user, role: str = Role.RECRUITER, **overrides):
    return CompanyMembership.objects.create(company=company, user=user, role=role, **overrides)


def company_with_owner(user, **overrides) -> tuple[Company, CompanyMembership]:
    company = create_company(**overrides)
    return company, add_member(company, user, Role.OWNER)
