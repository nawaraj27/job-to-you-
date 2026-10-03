"""
Settlement by bank transfer, recorded by a person (ADR-A15).

Not a stopgap. In Nepal a company paying for a hiring plan pays by transfer or
cheque and emails the receipt, and a billing system that cannot represent that
has not modelled the market it launched in. The manual provider is how that
payment becomes a row: a staff member records the reference and the amount,
and everything downstream — the invoice state, the audit entry, the event —
happens on exactly the same path as a gateway settlement.

`verify` refuses everything. There is no callback for a bank transfer; the
verification is a human reading a bank statement, and pretending otherwise
would give this provider an unverified path the real ones do not have.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .base import Charge, PaymentUnverified, Settlement, register


class ManualProvider:
    key = "manual"
    label = "Bank transfer"

    def start(self, *, invoice: Any) -> Charge:
        return Charge(
            reference=f"manual:{invoice.number}",
            instructions=(
                f"Transfer {invoice.currency} {invoice.total} quoting {invoice.number}, "
                "then send us the receipt. We record it against your account."
            ),
        )

    def verify(self, *, payload: dict[str, Any]) -> Settlement:
        raise PaymentUnverified(
            "A bank transfer is confirmed by a person, not by a callback. "
            "Record it through the staff screen."
        )

    def settle(self, *, invoice: Any, reference: str, amount: Decimal) -> Settlement:
        """What the staff screen calls once somebody has read the statement."""
        return Settlement(
            reference=reference,
            amount=amount,
            currency=invoice.currency,
            succeeded=True,
            detail={"method": "bank_transfer"},
        )


register(ManualProvider())
