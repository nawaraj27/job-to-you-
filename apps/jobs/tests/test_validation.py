"""
Input validation on a posting (SRS §5.7).

Every case here is a form field that arrives as a string from a browser and has
to become a typed column, and every one is asserted from the failing side. The
reason for the density: these refusals are the difference between an employer
being told "type a salary as a number" and a 500 page, and between a database
`CHECK` firing at the end of a long form and the form saying which field is
wrong while they are still looking at it.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.common.errors import InvalidState, NotFound
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import QuestionKind, Requirement
from apps.jobs.services import postings
from apps.jobs.tests.factories import create_job, create_skill, publish
from apps.taxonomy.models import JobCategory, Location


class TextFieldTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)
        self.job = create_job(self.company)

    def edit(self, **fields):
        return postings.update_job(job=self.job, actor=self.user, **fields)

    def test_a_blank_title_is_refused(self):
        with self.assertRaises(InvalidState):
            self.edit(title="   ")

    def test_a_long_title_is_truncated_rather_than_refused(self):
        # Truncation, not rejection: nobody should lose a whole form because a
        # title ran twelve characters long.
        self.edit(title="x" * 400)
        self.job.refresh_from_db()
        self.assertEqual(len(self.job.title), 160)

    def test_blank_lines_in_a_list_field_are_dropped(self):
        self.edit(responsibilities="One\n\n\n   \nTwo")
        self.job.refresh_from_db()
        self.assertEqual(self.job.responsibilities, ["One", "Two"])

    def test_a_list_field_also_accepts_a_real_list(self):
        self.edit(benefits=["Health cover", "  Learning budget  "])
        self.job.refresh_from_db()
        self.assertEqual(self.job.benefits, ["Health cover", "Learning budget"])


class ChoiceFieldTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)
        self.job = create_job(self.company)

    def edit(self, **fields):
        return postings.update_job(job=self.job, actor=self.user, **fields)

    def test_an_invented_workplace_is_refused(self):
        with self.assertRaises(InvalidState):
            self.edit(workplace="teleport")

    def test_an_invented_employment_type_is_refused(self):
        with self.assertRaises(InvalidState):
            self.edit(employment_type="indentured")

    def test_an_invented_experience_level_is_refused(self):
        with self.assertRaises(InvalidState):
            self.edit(experience_level="wizard")

    def test_an_invented_apply_mode_is_refused(self):
        with self.assertRaises(InvalidState):
            self.edit(apply_mode="carrier_pigeon")

    def test_remote_without_a_scope_is_refused_in_words(self):
        # The database CHECK would reject it anyway; this is the version an
        # employer can act on.
        with self.assertRaises(InvalidState) as caught:
            self.edit(workplace="remote", remote_scope="none")
        self.assertIn("remote hire", caught.exception.message)

    def test_remote_with_a_scope_is_accepted(self):
        self.edit(workplace="remote", remote_scope="worldwide")
        self.job.refresh_from_db()
        self.assertEqual(self.job.remote_scope, "worldwide")


class SalaryFieldTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)
        self.job = create_job(self.company)

    def edit(self, **fields):
        return postings.update_job(job=self.job, actor=self.user, **fields)

    def test_a_thousands_separator_is_accepted(self):
        self.edit(salary_min="120,000")
        self.job.refresh_from_db()
        self.assertEqual(self.job.salary_min, Decimal("120000.00"))

    def test_a_currency_symbol_is_refused_with_an_instruction(self):
        with self.assertRaises(InvalidState) as caught:
            self.edit(salary_min="Rs 120000")
        self.assertIn("without symbols", caught.exception.message)

    def test_a_negative_salary_is_refused(self):
        with self.assertRaises(InvalidState):
            self.edit(salary_min="-5000")

    def test_an_absurd_salary_is_refused_before_the_column_overflows(self):
        with self.assertRaises(InvalidState):
            self.edit(salary_min="99999999999999")

    def test_a_currency_code_is_upper_cased_and_clipped(self):
        self.edit(currency="npr")
        self.job.refresh_from_db()
        self.assertEqual(self.job.currency, "NPR")

    def test_pay_visibility_accepts_what_a_checkbox_posts(self):
        self.edit(salary_visible="on")
        self.job.refresh_from_db()
        self.assertTrue(self.job.salary_visible)

        self.edit(salary_visible=False)
        self.job.refresh_from_db()
        self.assertFalse(self.job.salary_visible)


class CooldownFieldTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)
        self.job = create_job(self.company)

    def test_a_cooldown_must_be_a_number(self):
        with self.assertRaises(InvalidState):
            postings.update_job(job=self.job, actor=self.user, application_cooldown_days="soon")

    def test_a_cooldown_beyond_a_year_is_refused(self):
        with self.assertRaises(InvalidState):
            postings.update_job(job=self.job, actor=self.user, application_cooldown_days="400")

    def test_an_empty_cooldown_means_none(self):
        postings.update_job(job=self.job, actor=self.user, application_cooldown_days="")
        self.job.refresh_from_db()
        self.assertEqual(self.job.application_cooldown_days, 0)


class ReferenceFieldTests(TestCase):
    """A foreign key posted as a string from a select."""

    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)
        self.job = create_job(self.company)

    def test_an_unknown_category_is_refused_by_name(self):
        with self.assertRaises(InvalidState) as caught:
            postings.update_job(job=self.job, actor=self.user, category_id="999999")
        self.assertIn("category", caught.exception.message)

    def test_an_unknown_location_is_refused_by_name(self):
        with self.assertRaises(InvalidState) as caught:
            postings.update_job(job=self.job, actor=self.user, location_id="999999")
        self.assertIn("location", caught.exception.message)

    def test_a_non_numeric_reference_is_refused(self):
        with self.assertRaises(InvalidState):
            postings.update_job(job=self.job, actor=self.user, category_id="engineering")

    def test_an_empty_select_clears_the_reference(self):
        category = JobCategory.objects.create(name="Engineering", slug="engineering")
        postings.update_job(job=self.job, actor=self.user, category_id=category.pk)
        postings.update_job(job=self.job, actor=self.user, category_id="")

        self.job.refresh_from_db()
        self.assertIsNone(self.job.category_id)

    def test_a_known_location_is_stored(self):
        place = Location.objects.create(country="NP", region="Bagmati", city="Kathmandu")
        postings.update_job(job=self.job, actor=self.user, location_id=str(place.pk))
        self.job.refresh_from_db()
        self.assertEqual(self.job.location_id, place.pk)


class SkillFieldTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)
        self.job = create_job(self.company)
        self.skill = create_skill("Python")

    def add(self, **kwargs):
        return postings.add_skill(job=self.job, actor=self.user, text="Python", **kwargs)

    def test_a_blank_skill_is_refused(self):
        with self.assertRaises(InvalidState):
            postings.add_skill(job=self.job, actor=self.user, text="   ")

    def test_an_invented_requirement_is_refused(self):
        with self.assertRaises(InvalidState):
            self.add(requirement="essential-ish")

    def test_a_weight_outside_zero_to_one_is_refused(self):
        with self.assertRaises(InvalidState):
            self.add(weight="5")

    def test_a_non_numeric_weight_is_refused(self):
        with self.assertRaises(InvalidState):
            self.add(weight="heavy")

    def test_an_omitted_weight_defaults_to_one(self):
        link = self.add()
        self.assertEqual(link.weight, Decimal("1.00"))

    def test_a_lifetime_of_experience_is_refused(self):
        with self.assertRaises(InvalidState):
            self.add(min_years="99")

    def test_a_non_numeric_experience_requirement_is_refused(self):
        with self.assertRaises(InvalidState):
            self.add(min_years="lots")

    def test_years_are_stored_to_one_decimal(self):
        link = self.add(min_years="2.55")
        self.assertEqual(link.min_years, Decimal("2.6"))

    def test_the_skill_list_is_capped(self):
        for index in range(postings.MAX_SKILLS):
            create_skill(f"Skill {index}")
            postings.add_skill(job=self.job, actor=self.user, text=f"Skill {index}")

        with self.assertRaises(InvalidState) as caught:
            self.add()
        self.assertIn("stops meaning anything", caught.exception.message)

    def test_removing_a_skill_that_is_not_there_is_not_found(self):
        with self.assertRaises(NotFound):
            postings.remove_skill(job=self.job, actor=self.user, skill_id=999999)

    def test_a_preferred_skill_is_stored_as_preferred(self):
        link = self.add(requirement=Requirement.PREFERRED)
        self.assertEqual(link.requirement, Requirement.PREFERRED)


class ScreeningQuestionTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(self.user)
        self.job = create_job(self.company)

    def add(self, **kwargs):
        kwargs.setdefault("text", "What is your notice period?")
        return postings.add_question(job=self.job, actor=self.user, **kwargs)

    def test_a_blank_question_is_refused(self):
        with self.assertRaises(InvalidState):
            self.add(text="   ")

    def test_an_invented_answer_kind_is_refused(self):
        with self.assertRaises(InvalidState):
            self.add(kind="interpretive_dance")

    def test_a_choice_question_needs_at_least_two_options(self):
        with self.assertRaises(InvalidState) as caught:
            self.add(kind=QuestionKind.SINGLE_CHOICE, options=["Only one"])
        self.assertIn("two options", caught.exception.message)

    def test_a_choice_question_with_options_is_accepted(self):
        question = self.add(kind=QuestionKind.MULTI_CHOICE, options=["Python", "Go", "  "])
        self.assertEqual(question.options, ["Python", "Go"])

    def test_questions_keep_the_order_they_were_added_in(self):
        first = self.add(text="First")
        second = self.add(text="Second")
        self.assertLess(first.order, second.order)

    def test_the_question_list_is_capped(self):
        for index in range(postings.MAX_QUESTIONS):
            self.add(text=f"Question {index}")
        with self.assertRaises(InvalidState):
            self.add(text="One too many")

    def test_removing_a_question_that_is_not_there_is_not_found(self):
        with self.assertRaises(NotFound):
            postings.remove_question(job=self.job, actor=self.user, question_id=999999)


class AssignmentValidationTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)
        self.job = create_job(self.company)

    def test_an_invented_assignment_kind_is_refused(self):
        with self.assertRaises(InvalidState):
            postings.assign(
                job=self.job, actor=self.user, membership=self.membership, kind="overlord"
            )

    def test_a_suspended_member_cannot_be_assigned(self):
        self.membership.status = self.membership.Status.SUSPENDED
        self.membership.save(update_fields=["status"])

        with self.assertRaises(InvalidState):
            postings.assign(job=self.job, actor=self.user, membership=self.membership)


class EditableStateTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)

    def test_a_published_posting_stays_published_while_it_is_edited(self):
        # Withdrawing a live role because somebody fixed a typo would lose it
        # its place in every candidate's saved list.
        job = publish(create_job(self.company))
        postings.update_job(job=job, actor=self.user, title="Corrected Title")

        job.refresh_from_db()
        self.assertEqual(job.status, "published")
        self.assertEqual(job.title, "Corrected Title")

    def test_a_closed_posting_cannot_be_edited_until_it_is_reopened(self):
        from apps.jobs.services import lifecycle

        job = publish(create_job(self.company))
        lifecycle.close(job=job, actor=self.user, membership=self.membership)

        with self.assertRaises(InvalidState):
            postings.update_job(job=job, actor=self.user, title="Too late")

    def test_an_edit_that_changes_nothing_is_refused_rather_than_silently_passing(self):
        job = create_job(self.company)
        with self.assertRaises(InvalidState):
            postings.update_job(job=job, actor=self.user)
