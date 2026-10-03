"""
Salary normalisation and FX provenance (ADR-A39).

The requirement that shapes all of this: a match made a year ago must still be
explicable and reproducible today. That rules out converting at read time, and
it rules out storing only the converted figure.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from apps.common.errors import InvalidState
from apps.taxonomy.models import FxRate
from apps.taxonomy.services import money


class AnnualiseTests(TestCase):
    def test_each_period_scales_to_a_year(self):
        cases = [
            ("year", Decimal("1200000"), Decimal("1200000.00")),
            ("month", Decimal("100000"), Decimal("1200000.00")),
            ("week", Decimal("10000"), Decimal("520000.00")),
            ("day", Decimal("5000"), Decimal("1300000.00")),
            ("hour", Decimal("500"), Decimal("1040000.00")),
        ]
        for period, amount, expected in cases:
            with self.subTest(period=period):
                self.assertEqual(money.annualise(amount, period), expected)

    def test_an_unknown_period_is_refused(self):
        with self.assertRaises(InvalidState):
            money.annualise(Decimal("100"), "fortnight")


class RateLookupTests(TestCase):
    def setUp(self):
        FxRate.objects.create(
            base="USD", quote="NPR", rate=Decimal("130"), as_of=dt.date(2026, 1, 1), source="test"
        )
        FxRate.objects.create(
            base="USD", quote="NPR", rate=Decimal("133"), as_of=dt.date(2026, 8, 1), source="test"
        )

    def test_the_rate_in_force_is_the_newest_not_after_the_date(self):
        # Converting a historical salary with a later rate would make the
        # figure move every time the table is refreshed.
        rate = money.rate_for(base="USD", quote="NPR", on=dt.date(2026, 3, 1))

        self.assertEqual(rate.rate, Decimal("130"))

    def test_the_latest_rate_applies_today(self):
        rate = money.rate_for(base="USD", quote="NPR", on=dt.date(2026, 8, 15))

        self.assertEqual(rate.rate, Decimal("133"))

    def test_a_missing_pair_raises_rather_than_guessing(self):
        # Falling back to 1.0 would make every salary comparison in that
        # currency quietly wrong, which nobody would ever notice.
        with self.assertRaises(InvalidState):
            money.rate_for(base="EUR", quote="NPR")

    def test_a_date_before_every_rate_raises(self):
        with self.assertRaises(InvalidState):
            money.rate_for(base="USD", quote="NPR", on=dt.date(2025, 1, 1))

    def test_a_stale_rate_is_refused(self):
        with self.assertRaises(InvalidState) as caught:
            money.rate_for(base="USD", quote="NPR", on=dt.date(2027, 6, 1))

        self.assertIn("days old", caught.exception.message)


class NormalisationTests(TestCase):
    def setUp(self):
        self.rate = FxRate.objects.create(
            base="USD",
            quote="NPR",
            rate=Decimal("133.25"),
            as_of=dt.date(2026, 8, 1),
            source="central-bank",
        )

    def test_the_headline_case_from_the_srs(self):
        # NPR 140,000/month and USD 45,000/year become comparable.
        local = money.normalise_salary(
            amount=Decimal("140000"), currency="NPR", period="month", on=dt.date(2026, 8, 15)
        )
        foreign = money.normalise_salary(
            amount=Decimal("45000"), currency="USD", period="year", on=dt.date(2026, 8, 15)
        )

        self.assertEqual(local.amount, Decimal("1680000.00"))
        self.assertEqual(foreign.amount, Decimal("5996250.00"))
        self.assertLess(local.amount, foreign.amount)

    def test_the_same_currency_needs_no_rate(self):
        result = money.normalise_salary(amount=Decimal("120000"), currency="NPR", period="month")

        self.assertIsNone(result.fx_rate_id)
        self.assertEqual(result.fx_source, "identity")

    def test_provenance_is_recorded(self):
        result = money.normalise_salary(
            amount=Decimal("45000"), currency="USD", period="year", on=dt.date(2026, 8, 15)
        )

        self.assertEqual(result.fx_rate_id, self.rate.pk)
        self.assertEqual(result.fx_rate_value, Decimal("133.25"))
        self.assertEqual(result.fx_as_of, dt.date(2026, 8, 1))
        self.assertEqual(result.fx_source, "central-bank")

    def test_the_original_figures_survive_conversion(self):
        # The interface shows what the employer typed, not the normalised value.
        result = money.normalise_salary(
            amount=Decimal("45000"), currency="USD", period="year", on=dt.date(2026, 8, 15)
        )

        self.assertEqual(result.source_amount, Decimal("45000"))
        self.assertEqual(result.source_currency, "USD")
        self.assertEqual(result.source_period, "year")

    def test_a_missing_amount_is_not_an_error(self):
        # An unstated salary is a legitimate state; forcing callers to special
        # case it invites a zero, which reads as "this job pays nothing".
        self.assertIsNone(money.normalise_salary(amount=None, currency="NPR", period="month"))

    def test_the_stored_fields_carry_everything_needed(self):
        result = money.normalise_salary(
            amount=Decimal("45000"), currency="USD", period="year", on=dt.date(2026, 8, 15)
        )

        fields = result.as_fields(prefix="salary_min")

        self.assertEqual(fields["salary_min_base"], Decimal("5996250.00"))
        self.assertEqual(fields["fx_rate_value"], Decimal("133.25"))
        self.assertEqual(fields["fx_source"], "central-bank")

    def test_a_conversion_reproduces_from_its_own_provenance(self):
        # The ADR-A39 test: recompute the figure a year later, from the values
        # stored beside it, without consulting the rate table at all.
        result = money.normalise_salary(
            amount=Decimal("45000"), currency="USD", period="year", on=dt.date(2026, 8, 15)
        )

        FxRate.objects.create(
            base="USD", quote="NPR", rate=Decimal("999"), as_of=dt.date(2026, 9, 1), source="later"
        )

        self.assertEqual(money.reproduce(result), result.amount)


class PrecisionTests(TestCase):
    """Money is Decimal end to end (ADR-A39).

    Floats would be close enough to pass a casual eye and wrong enough to make
    two salaries that are equal compare unequal — the kind of defect that
    surfaces as "why is this candidate filtered out" months later.
    """

    def setUp(self):
        self.rate = FxRate.objects.create(
            base="USD",
            quote="NPR",
            rate=Decimal("133.2567891234"),
            as_of=dt.date(2026, 8, 1),
            source="central-bank",
        )

    def test_conversion_keeps_every_digit_of_the_rate(self):
        result = money.normalise_salary(
            amount=Decimal("45000"), currency="USD", period="year", on=dt.date(2026, 8, 15)
        )

        # 45000 x 133.2567891234 = 5996555.510553, half-up to cents.
        self.assertEqual(result.amount, Decimal("5996555.51"))

    def test_fractional_hourly_pay_rounds_half_up_at_the_last_step(self):
        # Annualise first, convert once, round once. Rounding at each step
        # instead would drift by cents per row and by real money in aggregate.
        result = money.normalise_salary(
            amount=Decimal("12.345"), currency="USD", period="hour", on=dt.date(2026, 8, 15)
        )

        self.assertEqual(money.annualise(Decimal("12.345"), "hour"), Decimal("25677.60"))
        self.assertEqual(result.amount, Decimal("3421714.53"))

    def test_a_repeated_conversion_is_bit_for_bit_identical(self):
        first = money.normalise_salary(
            amount=Decimal("140000"), currency="USD", period="month", on=dt.date(2026, 8, 15)
        )
        second = money.normalise_salary(
            amount=Decimal("140000"), currency="USD", period="month", on=dt.date(2026, 8, 15)
        )

        self.assertEqual(first.amount, second.amount)
        self.assertEqual(money.reproduce(first), first.amount)

    def test_an_integer_amount_is_accepted_without_becoming_a_float(self):
        result = money.normalise_salary(
            amount=45000, currency="USD", period="year", on=dt.date(2026, 8, 15)
        )

        self.assertIsInstance(result.amount, Decimal)
        self.assertEqual(result.amount, Decimal("5996555.51"))


class InvalidInputTests(TestCase):
    def setUp(self):
        FxRate.objects.create(
            base="USD", quote="NPR", rate=Decimal("133"), as_of=dt.date(2026, 8, 1), source="test"
        )

    def test_a_malformed_currency_code_is_named_as_such(self):
        # "Rs" fails the rate lookup with the same message as a genuinely
        # missing rate, and the operator then hunts for the rate, not the typo.
        with self.assertRaises(InvalidState) as caught:
            money.normalise_salary(amount=Decimal("1000"), currency="Rs", period="month")

        self.assertIn("ISO 4217", caught.exception.message)

    def test_an_empty_currency_is_refused(self):
        with self.assertRaises(InvalidState):
            money.normalise_salary(amount=Decimal("1000"), currency="", period="month")

    def test_a_numeric_currency_code_is_refused(self):
        with self.assertRaises(InvalidState):
            money.normalise_salary(amount=Decimal("1000"), currency="524", period="month")

    def test_an_unknown_but_well_formed_currency_reports_a_missing_rate(self):
        # Different failure, different message: the code is fine, the data is
        # absent, and loading rates is the fix.
        with self.assertRaises(InvalidState) as caught:
            money.normalise_salary(
                amount=Decimal("1000"), currency="XAF", period="month", on=dt.date(2026, 8, 15)
            )

        self.assertIn("No exchange rate", caught.exception.message)

    def test_a_malformed_target_currency_is_refused(self):
        with self.assertRaises(InvalidState):
            money.normalise_salary(
                amount=Decimal("1000"), currency="USD", period="month", to_currency="rupees"
            )

    def test_the_rate_lookup_validates_both_sides(self):
        with self.assertRaises(InvalidState):
            money.rate_for(base="US", quote="NPR")
        with self.assertRaises(InvalidState):
            money.rate_for(base="USD", quote="N")

    def test_an_invalid_period_is_refused_before_any_rate_is_read(self):
        with self.assertRaises(InvalidState) as caught:
            money.normalise_salary(amount=Decimal("1000"), currency="USD", period="fortnight")

        self.assertIn("fortnight", caught.exception.message)

    def test_a_lowercase_currency_is_accepted_and_folded(self):
        result = money.normalise_salary(
            amount=Decimal("1000"), currency="usd", period="year", on=dt.date(2026, 8, 15)
        )

        self.assertEqual(result.source_currency, "USD")


class AppendOnlyTests(TestCase):
    def test_a_rate_cannot_be_edited(self):
        # Editing in place silently rewrites every conversion that used it.
        rate = FxRate.objects.create(
            base="USD", quote="NPR", rate=Decimal("133"), as_of=dt.date(2026, 8, 1), source="test"
        )
        rate.rate = Decimal("200")

        with self.assertRaises(ValueError):
            rate.save()

    def test_currency_codes_are_stored_upper_case(self):
        rate = FxRate.objects.create(
            base="usd", quote="npr", rate=Decimal("133"), as_of=dt.date(2026, 8, 2), source="test"
        )

        self.assertEqual((rate.base, rate.quote), ("USD", "NPR"))

    def test_one_rate_per_pair_per_day(self):
        from django.db import IntegrityError

        FxRate.objects.create(
            base="USD", quote="NPR", rate=Decimal("133"), as_of=dt.date(2026, 8, 3), source="test"
        )
        with self.assertRaises(IntegrityError):
            FxRate.objects.create(
                base="USD",
                quote="NPR",
                rate=Decimal("134"),
                as_of=dt.date(2026, 8, 3),
                source="test",
            )


class LoadRatesCommandTests(TestCase):
    def test_a_rate_loads_from_arguments(self):
        call_command(
            "load_fx_rates", "--pair", "USD:NPR", "--rate", "133.25", "--as-of", "2026-08-01"
        )

        self.assertTrue(FxRate.objects.filter(base="USD", quote="NPR").exists())

    def test_reloading_the_same_day_is_skipped_not_overwritten(self):
        call_command(
            "load_fx_rates", "--pair", "USD:NPR", "--rate", "133.25", "--as-of", "2026-08-01"
        )
        call_command(
            "load_fx_rates", "--pair", "USD:NPR", "--rate", "999.00", "--as-of", "2026-08-01"
        )

        self.assertEqual(FxRate.objects.get(base="USD", quote="NPR").rate, Decimal("133.25"))
