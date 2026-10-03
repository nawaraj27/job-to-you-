"""
Search (SRS §5.7).

Three properties are worth a test each, and all three are things a job board
gets quietly wrong:

  * **the generated column tracks the row.** `search_vector` is computed by
    Postgres, so an edit that did not update it is impossible rather than
    merely unlikely — and the test proves the column really is generated;
  * **ranking is stable.** Two equally relevant rows must not swap between
    pages, because the symptom is a job that appears on neither;
  * **filters are boundaries.** A salary floor is not a preference, and a
    result below it means the search ignored the person.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import EmploymentType, Job, Workplace
from apps.jobs.search import SearchQueryInput, matches_query, search
from apps.jobs.tests.factories import create_job, publish


class GeneratedColumnTests(TestCase):
    def setUp(self):
        self.company, _ = company_with_owner(create_user())

    def test_the_search_vector_is_populated_without_anyone_writing_it(self):
        job = publish(create_job(self.company, title="Django Developer"))
        job.refresh_from_db()
        self.assertIsNotNone(job.search_vector)

    def test_editing_the_title_updates_the_vector(self):
        job = publish(create_job(self.company, title="Django Developer"))
        self.assertEqual(search(SearchQueryInput(text="django")).total, 1)

        Job.objects.filter(pk=job.pk).update(title="Rust Engineer")

        # The column is generated, so it cannot disagree with the row it
        # describes — no trigger, no save() hook, no second source of truth.
        self.assertEqual(search(SearchQueryInput(text="django")).total, 0)
        self.assertEqual(search(SearchQueryInput(text="rust")).total, 1)


class FullTextTests(TestCase):
    def setUp(self):
        self.company, _ = company_with_owner(create_user())
        self.python = publish(
            create_job(
                self.company,
                title="Senior Python Engineer",
                description="Django, PostgreSQL and a large billing service.",
            )
        )
        self.design = publish(
            create_job(
                self.company,
                title="Product Designer",
                description="Figma, design systems and research.",
            )
        )

    def test_a_title_word_finds_the_posting(self):
        result = search(SearchQueryInput(text="python"))
        self.assertEqual([job.pk for job in result.jobs], [self.python.pk])

    def test_a_description_word_finds_the_posting(self):
        result = search(SearchQueryInput(text="figma"))
        self.assertEqual([job.pk for job in result.jobs], [self.design.pk])

    def test_a_draft_is_never_searchable(self):
        create_job(self.company, title="Python Intern")
        self.assertEqual(search(SearchQueryInput(text="python")).total, 1)

    def test_a_typo_still_finds_the_role(self):
        result = search(SearchQueryInput(text="Pythn Engineer"))
        self.assertTrue(result.used_trigram)
        self.assertIn(self.python, list(result.jobs))

    def test_nonsense_finds_nothing_rather_than_everything(self):
        self.assertEqual(search(SearchQueryInput(text="qzxwvy")).total, 0)


class FilterTests(TestCase):
    def setUp(self):
        self.company, _ = company_with_owner(create_user())
        self.remote = publish(
            create_job(
                self.company,
                title="Remote Backend Engineer",
                workplace=Workplace.REMOTE,
                remote_scope="worldwide",
                employment_type=EmploymentType.CONTRACT,
                salary_min=Decimal("200000"),
                salary_max=Decimal("300000"),
                currency="NPR",
                period="year",
            )
        )
        self.onsite = publish(
            create_job(
                self.company,
                title="Onsite Backend Engineer",
                workplace=Workplace.ON_SITE,
                salary_min=Decimal("40000"),
                salary_max=Decimal("60000"),
                currency="NPR",
                period="year",
            )
        )

    def test_a_workplace_filter_excludes_the_others(self):
        result = search(SearchQueryInput(workplace=Workplace.REMOTE))
        self.assertEqual([job.pk for job in result.jobs], [self.remote.pk])

    def test_an_employment_type_filter_excludes_the_others(self):
        result = search(SearchQueryInput(employment_type=EmploymentType.CONTRACT))
        self.assertEqual([job.pk for job in result.jobs], [self.remote.pk])

    def test_a_salary_floor_is_a_boundary_and_not_a_preference(self):
        # Built through the service, because the comparison runs on the
        # annualised figure the service derives — a row written straight to the
        # table has no such figure, which is the next test.
        from apps.jobs.services import postings

        user = self.company.memberships.first().user
        rich = publish(
            postings.create_job(
                company=self.company,
                actor=user,
                title="Staff Engineer",
                description="Senior role.",
                salary_min="400000",
                salary_max="600000",
                currency="NPR",
                period="year",
            )
        )
        result = search(SearchQueryInput(salary_min=Decimal("300000")))
        self.assertEqual([job.pk for job in result.jobs], [rich.pk])

    def test_a_posting_with_no_comparable_figure_is_excluded_by_a_floor(self):
        hidden = publish(
            create_job(self.company, salary_min=None, salary_max=None, salary_visible=False)
        )
        # Excluded rather than assumed to qualify: the alternative wastes the
        # time of exactly the person who set a floor.
        result = search(SearchQueryInput(salary_min=Decimal("1")))
        self.assertNotIn(hidden.pk, [job.pk for job in result.jobs])

    def test_facets_count_the_rows_the_search_returned(self):
        result = search(SearchQueryInput())
        counts = {row["value"]: row["count"] for row in result.facets["workplace"]}
        self.assertEqual(counts[Workplace.REMOTE], 1)
        self.assertEqual(counts[Workplace.ON_SITE], 1)

    def test_a_facet_never_offers_a_choice_that_returns_nothing(self):
        result = search(SearchQueryInput(workplace=Workplace.REMOTE))
        for row in result.facets["workplace"]:
            self.assertGreater(row["count"], 0)


class OrderingTests(TestCase):
    def setUp(self):
        self.company, _ = company_with_owner(create_user())

    def test_ranking_is_stable_across_identical_rows(self):
        stamp = timezone.now() - dt.timedelta(days=1)
        for _ in range(5):
            job = publish(create_job(self.company, title="Backend Engineer"))
            Job.objects.filter(pk=job.pk).update(published_at=stamp)

        first = [job.pk for job in search(SearchQueryInput(text="backend")).jobs]
        second = [job.pk for job in search(SearchQueryInput(text="backend")).jobs]
        # Without the final tiebreak these can swap between pages, and a job
        # ends up on neither.
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), 5)

    def test_newest_first_orders_by_publication(self):
        older = publish(create_job(self.company, title="Older Role"))
        Job.objects.filter(pk=older.pk).update(published_at=timezone.now() - dt.timedelta(days=9))
        newer = publish(create_job(self.company, title="Newer Role"))

        result = search(SearchQueryInput(sort="newest"))
        self.assertEqual([job.pk for job in result.jobs][0], newer.pk)

    def test_sorting_by_salary_puts_the_highest_first(self):
        low = publish(create_job(self.company, salary_min=Decimal("10"), salary_max=Decimal("20")))
        high = publish(
            create_job(self.company, salary_min=Decimal("900000"), salary_max=Decimal("990000"))
        )
        result = search(SearchQueryInput(sort="salary"))
        order = [job.pk for job in result.jobs]
        self.assertLess(order.index(high.pk), order.index(low.pk))


class QueryInputTests(TestCase):
    """A saved search stores what was chosen, not the URL it came from."""

    def test_a_malformed_filter_is_dropped_rather_than_fatal(self):
        query = SearchQueryInput.from_mapping(
            {"workplace": "teleport", "salary_min": "not a number", "location": "abc"}
        )
        self.assertEqual(query.workplace, "")
        self.assertIsNone(query.salary_min)
        self.assertIsNone(query.location_id)

    def test_a_round_trip_keeps_only_what_was_actually_chosen(self):
        query = SearchQueryInput.from_mapping({"q": "python", "workplace": "remote"})
        restored = SearchQueryInput.from_mapping(query.as_mapping())
        self.assertEqual(restored.text, "python")
        self.assertEqual(restored.workplace, "remote")
        self.assertEqual(restored.employment_type, "")

    def test_an_empty_search_knows_it_is_empty(self):
        self.assertTrue(SearchQueryInput.from_mapping({}).is_empty)
        self.assertFalse(SearchQueryInput.from_mapping({"q": "python"}).is_empty)


class MatchesQueryTests(TestCase):
    """The alert batch asks the same question the screen asked."""

    def setUp(self):
        self.company, _ = company_with_owner(create_user())
        self.job = publish(create_job(self.company, title="Python Engineer"))

    def test_a_matching_job_matches(self):
        self.assertTrue(matches_query(self.job, SearchQueryInput(text="python")))

    def test_a_non_matching_job_does_not(self):
        self.assertFalse(matches_query(self.job, SearchQueryInput(text="accountant")))
