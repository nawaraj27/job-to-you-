"""
How an amount of money is written, everywhere (docs/billing/Pricing-and-Quotas.md).

One function, so a price on the pricing page, an invoice row, a checkout page
and an email can never disagree about how NPR 1,700 looks. Templates use the
`money` filter (`apps.core.templatetags.ui`), which calls this.

Rupees are written "NPR 1,700": the code is unambiguous where "Rs" is shared
by several currencies, and it is what the payment wallets show. Whole amounts
drop their paise; others keep two places.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

#: Currency code -> how it is written before the number.
PREFIX = {"NPR": "NPR ", "USD": "US$", "INR": "INR "}


def format_money(amount: Any, currency: str = "NPR") -> str:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return f"{currency} {amount}"
    code = (currency or "NPR").upper()
    prefix = PREFIX.get(code, f"{code} ")
    if value == value.to_integral_value():
        number = f"{int(value):,}"
    else:
        number = f"{value.quantize(Decimal('0.01')):,}"
    return f"{prefix}{number}"
