"""
Demo recruiters are put on their company's postings (decision D-13).

Recruiters reach applicants only on assigned jobs, so demo data that never
assigned anybody showed every demo recruiter an empty applicant board.
"""

from __future__ import annotations

from django.test import TestCase

from apps.candidates.tests.factories import create_profile
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.core.management.commands.seed_demo import Command
from apps.jobs.models import JobAssignment
from apps.jobs.tests.factories import create_job, publish


class AssignRecruitersTests(TestCase):
    def test_recruiters_are_assigned_once_and_only_in_their_company(self):
        owner = create_profile().user
        company, _ = company_with_owner(owner)
        recruiter = add_member(company, create_profile().user, role=Role.RECRUITER)
        viewer = add_member(company, create_profile().user, role=Role.VIEWER)
        other_company, _ = company_with_owner(create_profile().user)
        outsider = add_member(other_company, create_profile().user, role=Role.RECRUITER)
        job = publish(create_job(company))

        Command()._assign_recruiters([job])
        Command()._assign_recruiters([job])

        self.assertEqual(JobAssignment.objects.filter(job=job, membership=recruiter).count(), 1)
        self.assertFalse(JobAssignment.objects.filter(membership=viewer).exists())
        self.assertFalse(JobAssignment.objects.filter(membership=outsider).exists())
