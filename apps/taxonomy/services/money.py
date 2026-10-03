"""
Salary normalisation with provenance (ADR-A39, SRS §5.3).

A job paying NPR 140,000 per month and a candidate expecting USD 45,000 per year
cannot be compared until both are annualised in one base currency. That much is
obvious. The part that is easy to skip — and that this module exists to prevent
— is recording *which rate did it*.

Without the rate, its date and its source, a match made last March cannot be
explained or reproduced this March, and "why did this job match my salary band"
is a question both sides of a marketplace ask. So conversion returns a result
carrying its own provenance, and callers persist all of it.

A missing rate raises. Falling back to 1.0, or to the nearest rate whatever its
age, would produce salary comparisons that are quietly wrong everywhere — the
worst possible failure for a number nobody re-checks.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.errors import InvalidState

from ..models import FxRate

#: The currency every comparison happens in.
BASE_CURRENCY = "NPR"

#: How stale a rate may be before conversion refuses. Currencies move; a
#: two-year-old rate is not a conversion, it is a guess with a decimal point.
MAX_RATE_AGE_DAYS = 90


class Period(models.TextChoices):
    HOUR = "hour", _("Per hour")
    DAY = "day", _("Per day")
    WEEK = "week", _("Per week")
    MONTH = "month", _("Per month")
    YEAR = "year", _("Per year")


#: Full-time equivalents. Stated here once, because a matcher that annualises
#: hourly pay at 2,000 hours and a report that uses 1,800 disagree forever.
ANNUAL_MULTIPLIER: dict[str, Decimal] = {
    Period.HOUR: Decimal("2080"),  # 40h x 52w
    Period.DAY: Decimal("260"),  # 5d x 52w
    Period.WEEK: Decimal("52"),
    Period.MONTH: Decimal("12"),
    Period.YEAR: Decimal("1"),
}

CENTS = Decimal("0.01")

#: ISO 4217 is three letters. Checked because "Rs", "NPR " and "" all fail the
#: rate lookup with the same message as a genuinely missing rate, and an
#: operator then goes looking for the rate instead of for the typo.
_CURRENCY_LENGTH = 3


def _validate_currency(code: str, *, label: str) -> str:
    cleaned = (code or "").strip().upper()
    if len(cleaned) != _CURRENCY_LENGTH or not cleaned.isalpha():
        raise InvalidState(f"{label} {code!r} is not an ISO 4217 currency code.")
    return cleaned


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class NormalisedSalary:
    """An annualised figure and everything needed to reproduce it."""

    amount: Decimal
    currency: str
    source_amount: Decimal
    source_currency: str
    source_period: str
    fx_rate_id: int | None
    fx_rate_value: Decimal
    fx_as_of: dt.date | None
    fx_source: str

    def as_fields(self, prefix: str = "salary") -> dict[str, object]:
        """The columns a caller stores alongside the original figures.

        `fx_rate_value` is denormalised deliberately: the foreign key alone is
        not enough if the rate table is ever pruned or re-sourced, and the
        figure that was actually used has to survive independently of the row
        it came from.
        """
        return {
            f"{prefix}_base": self.amount,
            "fx_rate_id": self.fx_rate_id,
            "fx_rate_value": self.fx_rate_value,
            "fx_as_of": self.fx_as_of,
            "fx_source": self.fx_source,
        }


def annualise(amount: Decimal, period: str) -> Decimal:
    """Scale a figure to a year at full-time equivalence."""
    try:
        multiplier = ANNUAL_MULTIPLIER[period]
    except KeyError:
        raise InvalidState(f"Unknown pay period: {period!r}") from None
    return (Decimal(amount) * multiplier).quantize(CENTS, rounding=ROUND_HALF_UP)


def rate_for(
    *, base: str, quote: str, on: dt.date | None = None, max_age_days: int = MAX_RATE_AGE_DAYS
) -> FxRate:
    """The rate in force for a pair on a date.

    "In force" means the newest rate not after that date — never a later one.
    Converting a historical salary with tomorrow's rate would make the figure
    move every time the table is refreshed.
    """
    base = _validate_currency(base, label="Base currency")
    quote = _validate_currency(quote, label="Quote currency")
    on = on or dt.date.today()

    rate = FxRate.objects.filter(base=base, quote=quote, as_of__lte=on).order_by("-as_of").first()
    if rate is None:
        raise InvalidState(
            f"No exchange rate for {base}/{quote} on or before {on}. "
            "Load rates before comparing salaries in this currency."
        )

    age = (on - rate.as_of).days
    if age > max_age_days:
        raise InvalidState(
            f"The newest {base}/{quote} rate is {age} days old ({rate.as_of}). "
            "Refusing to convert rather than guess."
        )
    return rate


def normalise_salary(
    *,
    amount: Decimal | int | None,
    currency: str,
    period: str,
    on: dt.date | None = None,
    to_currency: str = BASE_CURRENCY,
) -> NormalisedSalary | None:
    """Annualise and convert, keeping the provenance.

    Returns None for a missing amount — an unstated salary is a legitimate
    state, and forcing callers to special-case it invites a zero instead.
    """
    if amount is None:
        return None

    source_amount = Decimal(amount)
    currency = _validate_currency(currency, label="Currency")
    to_currency = _validate_currency(to_currency, label="Target currency")
    annual = annualise(source_amount, period)

    if currency == to_currency:
        # Same currency: no rate is involved, and recording a fictional 1.0
        # from a nonexistent source would be worse than recording nothing.
        return NormalisedSalary(
            amount=annual,
            currency=to_currency,
            source_amount=source_amount,
            source_currency=currency,
            source_period=period,
            fx_rate_id=None,
            fx_rate_value=Decimal("1"),
            fx_as_of=None,
            fx_source="identity",
        )

    rate = rate_for(base=currency, quote=to_currency, on=on)
    converted = (annual * rate.rate).quantize(CENTS, rounding=ROUND_HALF_UP)

    return NormalisedSalary(
        amount=converted,
        currency=to_currency,
        source_amount=source_amount,
        source_currency=currency,
        source_period=period,
        fx_rate_id=rate.pk,
        fx_rate_value=rate.rate,
        fx_as_of=rate.as_of,
        fx_source=rate.source,
    )


def reproduce(normalised: NormalisedSalary) -> Decimal:
    """Recompute a stored conversion from its own provenance.

    The test that matters for ADR-A39: a figure normalised a year ago must come
    back identical today, from the values stored beside it, without consulting
    the rate table at all.
    """
    annual = annualise(normalised.source_amount, normalised.source_period)
    return (annual * normalised.fx_rate_value).quantize(CENTS, rounding=ROUND_HALF_UP)
