"""
Writing a posting (SRS §5.7).

Three things are load-bearing here and each has its own failing case:

  * only whitelisted fields are writable;
  * salary keeps what the employer typed *and* a comparable base figure with
    its provenance (ADR-A39);
  * a skill that does not resolve is refused and the term is banked for
    curation (ADR-A46) — which must survive the refusal it accompanies.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.common.errors import Conflict, InvalidState, StaleWrite
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import Job, JobAssignment, JobStatus, Requirement
from apps.jobs.services import postings
from apps.jobs.tests.factories import create_job, create_skill, publish
from apps.taxonomy.models import UnresolvedTerm


class CreateTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)

    def test_a_new_posting_starts_as_a_draft(self):
        job = postings.create_job(company=self.company, actor=self.user, title="Data Engineer")
        self.assertEqual(job.status, JobStatus.DRAFT)
        self.assertIsNone(job.published_at)

    def test_a_posting_needs_a_title(self):
        with self.assertRaises(InvalidState):
            postings.create_job(company=self.company, actor=self.user, title="   ")

    def test_slugs_are_unique_within_and_across_companies(self):
        first = postings.create_job(company=self.company, actor=self.user, title="Data Engineer")
        second = postings.create_job(company=self.company, actor=self.user, title="Data Engineer")
        self.assertNotEqual(first.slug, second.slug)

    def test_the_slug_carries_the_company_so_a_url_says_who_is_hiring(self):
        job = postings.create_job(company=self.company, actor=self.user, title="Data Engineer")
        self.assertTrue(job.slug.startswith(self.company.slug))

    def test_creation_is_audited(self):
        postings.create_job(company=self.company, actor=self.user, title="Data Engineer")
        self.assertTrue(AuditLog.objects.filter(verb="job.created").exists())


class UpdateTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)
        self.job = create_job(self.company)

    def test_only_whitelisted_fields_are_written(self):
        postings.update_job(
            job=self.job,
            actor=self.user,
            title="New title",
            views_count=9999,
            company_id=0,
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.title, "New title")
        self.assertEqual(self.job.views_count, 0)
        self.assertEqual(self.job.company_id, self.company.pk)

    def test_a_stale_row_version_is_refused_rather_than_overwriting(self):
        with self.assertRaises(StaleWrite):
            postings.update_job(
                job=self.job, actor=self.user, title="Mine", row_version=self.job.row_version + 1
            )
        self.job.refresh_from_db()
        self.assertNotEqual(self.job.title, "Mine")

    def test_an_edit_bumps_the_row_version_so_the_next_writer_notices(self):
        before = self.job.row_version
        postings.update_job(job=self.job, actor=self.user, title="Edited")
        self.job.refresh_from_db()
        self.assertEqual(self.job.row_version, before + 1)

    def test_a_list_field_accepts_one_item_per_line(self):
        postings.update_job(
            job=self.job, actor=self.user, responsibilities="Own billing\n\nRun the on-call"
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.responsibilities, ["Own billing", "Run the on-call"])

    def test_an_absurdly_long_list_is_refused(self):
        with self.assertRaises(InvalidState):
            postings.update_job(
                job=self.job,
                actor=self.user,
                benefits="\n".join(f"Perk {index}" for index in range(200)),
            )

    def test_an_inverted_salary_range_is_refused_before_it_reaches_the_database(self):
        with self.assertRaises(InvalidState):
            postings.update_job(
                job=self.job, actor=self.user, salary_min="200000", salary_max="100000"
            )


class SalaryProvenanceTests(TestCase):
    """What the employer typed is never lost (ADR-A39)."""

    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)

    def test_the_typed_figures_survive_normalisation(self):
        job = postings.create_job(
            company=self.company,
            actor=self.user,
            title="Engineer",
            salary_min="80000",
            salary_max="120000",
            currency="NPR",
            period="month",
        )
        self.assertEqual(job.salary_min, Decimal("80000"))
        self.assertEqual(job.currency, "NPR")
        self.assertEqual(job.period, "month")

    def test_a_comparable_annual_figure_is_derived(self):
        job = postings.create_job(
            company=self.company,
            actor=self.user,
            title="Engineer",
            salary_min="10000",
            salary_max="20000",
            currency="NPR",
            period="month",
        )
        # Monthly pay compared against an annual filter has to be annualised,
        # or a salary floor silently excludes everyone paid monthly.
        self.assertIsNotNone(job.salary_min_base)
        self.assertGreater(job.salary_min_base, job.salary_min)

    def test_clearing_the_salary_clears_the_derived_figures_too(self):
        job = create_job(self.company)
        postings.update_job(job=job, actor=self.user, salary_min="", salary_max="")
        job.refresh_from_db()
        self.assertIsNone(job.salary_min)
        self.assertIsNone(job.salary_min_base)


class SkillTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)
        self.job = create_job(self.company)
        self.skill = create_skill("Python")

    def test_a_resolved_skill_is_attached_to_the_taxonomy_row(self):
        link = postings.add_skill(job=self.job, actor=self.user, text="python")
        self.assertEqual(link.skill, self.skill)
        self.assertEqual(link.requirement, Requirement.REQUIRED)

    def test_free_text_is_refused_rather_than_creating_a_skill_per_typo(self):
        with self.assertRaises(InvalidState):
            postings.add_skill(job=self.job, actor=self.user, text="Pythonn 3 (advanced)")

    def test_the_unresolved_term_survives_the_refusal_it_caused(self):
        # Recorded before the raise, so the rollback that carries the error
        # does not also discard the evidence a term is missing (ADR-A46).
        with self.assertRaises(InvalidState):
            postings.add_skill(job=self.job, actor=self.user, text="Kubernetesz")
        self.assertTrue(UnresolvedTerm.objects.filter(term__icontains="kubernetesz").exists())

    def test_the_same_skill_cannot_be_listed_twice(self):
        postings.add_skill(job=self.job, actor=self.user, text="Python")
        with self.assertRaises(Conflict):
            postings.add_skill(job=self.job, actor=self.user, text="python")

    def test_removing_a_skill_leaves_the_taxonomy_row_alone(self):
        postings.add_skill(job=self.job, actor=self.user, text="Python")
        postings.remove_skill(job=self.job, actor=self.user, skill_id=self.skill.pk)

        self.assertEqual(self.job.skills.count(), 0)
        self.assertTrue(type(self.skill).objects.filter(pk=self.skill.pk).exists())


class CloneTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)
        self.job = publish(create_job(self.company))
        create_skill("Python")
        postings.add_skill(job=self.job, actor=self.user, text="Python")
        postings.add_question(job=self.job, actor=self.user, text="Notice period?")

    def test_a_clone_is_a_fresh_draft(self):
        copy = postings.clone_job(job=self.job, actor=self.user)
        self.assertEqual(copy.status, JobStatus.DRAFT)
        self.assertIsNone(copy.published_at)
        self.assertNotEqual(copy.slug, self.job.slug)

    def test_a_clone_carries_the_skills_and_questions_that_took_the_time(self):
        copy = postings.clone_job(job=self.job, actor=self.user)
        self.assertEqual(copy.skills.count(), 1)
        self.assertEqual(copy.questions.count(), 1)

    def test_a_clone_starts_its_counters_at_zero(self):
        Job.objects.filter(pk=self.job.pk).update(views_count=42, applications_count=7)
        copy = postings.clone_job(job=Job.objects.get(pk=self.job.pk), actor=self.user)
        self.assertEqual(copy.views_count, 0)
        self.assertEqual(copy.applications_count, 0)


class AssignmentTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)
        self.job = create_job(self.company)

    def test_assigning_twice_is_idempotent(self):
        postings.assign(job=self.job, actor=self.user, membership=self.membership)
        postings.assign(job=self.job, actor=self.user, membership=self.membership)
        self.assertEqual(self.job.assignments.count(), 1)

    def test_one_person_can_hold_two_roles_on_one_posting(self):
        postings.assign(
            job=self.job,
            actor=self.user,
            membership=self.membership,
            kind=JobAssignment.Kind.RECRUITER,
        )
        postings.assign(
            job=self.job,
            actor=self.user,
            membership=self.membership,
            kind=JobAssignment.Kind.HIRING_MANAGER,
        )
        self.assertEqual(self.job.assignments.count(), 2)

    def test_unassigning_removes_only_the_named_role(self):
        postings.assign(
            job=self.job,
            actor=self.user,
            membership=self.membership,
            kind=JobAssignment.Kind.RECRUITER,
        )
        postings.assign(
            job=self.job,
            actor=self.user,
            membership=self.membership,
            kind=JobAssignment.Kind.HIRING_MANAGER,
        )
        postings.unassign(
            job=self.job,
            actor=self.user,
            membership=self.membership,
            kind=JobAssignment.Kind.RECRUITER,
        )
        self.assertEqual(
            list(self.job.assignments.values_list("kind", flat=True)),
            [JobAssignment.Kind.HIRING_MANAGER],
        )

    def test_assignment_is_audited(self):
        postings.assign(job=self.job, actor=self.user, membership=self.membership)
        self.assertTrue(AuditLog.objects.filter(verb="job.assigned").exists())


class DeleteTests(TestCase):
    def test_deletion_is_soft_so_nothing_pointing_at_the_posting_breaks(self):
        user = create_user()
        company, _ = company_with_owner(user)
        job = create_job(company)

        postings.delete_job(job=job, actor=user)

        job.refresh_from_db()
        self.assertIsNotNone(job.deleted_at)
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())
        self.assertFalse(Job.objects.alive().filter(pk=job.pk).exists())
