"""
A gateway that behaves like a gateway, for tests and for local work.

It exists so that the *callback* path — the one a real provider will use, and
the one that is most dangerous to get wrong — is exercised by the test suite
rather than only by the first live payment. It therefore signs its payloads
with an HMAC the way every payment gateway does, and `verify` refuses a payload
whose signature does not match, whose amount was edited in transit, or that
names an invoice it was not issued for.

A test can make it fail on demand (`fail_next`), because a settlement that
never fails is not a settlement path anybody has tested.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import Any

from django.conf import settings

from .base import Charge, PaymentUnverified, Settlement, register

#: Only ever used by this provider, and only in dev and tests. A real provider
#: reads its secret from the environment via `settings`.
FAKE_SECRET = "fake-provider-secret"  # noqa: S105 - a test fixture, not a credential


def _secret() -> str:
    return getattr(settings, "BILLING_FAKE_PROVIDER_SECRET", FAKE_SECRET)


def sign(payload: dict[str, Any]) -> str:
    """The signature a gateway would send.

    Over a canonical subset rather than the whole body: a signature over
    everything breaks the moment the provider adds a field, and a receiver that
    responds by skipping verification is worse off than one that never had it.
    """
    # The currency is signed with the amount: an amount whose currency can be
    # edited in transit is not a verified amount.
    message = (
        f"{payload.get('reference', '')}|{payload.get('invoice', '')}|"
        f"{payload.get('amount', '')}|{payload.get('currency', '')}"
    )
    return hmac.new(_secret().encode(), message.encode(), hashlib.sha256).hexdigest()


class FakeProvider:
    key = "fake"
    label = "Test gateway"

    def __init__(self) -> None:
        self.fail_next = False
        self.started: list[str] = []

    def reset(self) -> None:
        self.fail_next = False
        self.started = []

    def start(self, *, invoice: Any) -> Charge:
        reference = f"fake:{invoice.number}"
        self.started.append(reference)
        return Charge(
            reference=reference,
            redirect_url=f"https://example.invalid/pay/{invoice.number}",
            detail={"amount": str(invoice.total)},
        )

    def callback_for(self, *, invoice: Any, succeeded: bool = True) -> dict[str, Any]:
        """The body the gateway would post back. Signed, as a real one is."""
        payload = {
            "reference": f"fake:{invoice.number}",
            "invoice": invoice.number,
            "amount": str(invoice.total),
            "currency": invoice.currency,
            "status": "ok" if succeeded else "failed",
        }
        return {**payload, "signature": sign(payload)}

    def verify(self, *, payload: dict[str, Any]) -> Settlement:
        expected = sign(payload)
        given = str(payload.get("signature", ""))
        if not hmac.compare_digest(expected, given):
            raise PaymentUnverified("That payment confirmation did not carry a valid signature.")
        if self.fail_next:
            self.fail_next = False
            return Settlement(
                reference=str(payload["reference"]),
                amount=Decimal(str(payload["amount"])),
                currency=str(payload.get("currency", "")),
                succeeded=False,
                detail={"reason": "declined"},
            )

        return Settlement(
            reference=str(payload["reference"]),
            amount=Decimal(str(payload["amount"])),
            # No default: a gateway that does not say which currency it took
            # has not told us how much it took.
            currency=str(payload.get("currency", "")),
            succeeded=payload.get("status") == "ok",
            detail={"invoice": payload.get("invoice", "")},
        )


#: One shared instance so a test can arm `fail_next` on the object the code
#: will actually use, the same way `ai.providers.fake` works.
provider = FakeProvider()
register(provider)
