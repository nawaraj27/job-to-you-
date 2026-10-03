"""
The payment provider interface (ADR-A15).

Nepal-first: eSewa, Khalti and Fonepay are the providers this product needs,
and Stripe is added on market entry rather than swapped in. That is only true
if every one of them answers the same two questions, which is what this
protocol is:

  * **`start`** — given an invoice, what should the customer be sent to, and
    what reference will this settlement be known by?
  * **`verify`** — given whatever the provider posted back, is this a genuine
    settlement of that invoice, and for how much?

Nothing else is in the protocol. Refunds, subscriptions-as-a-provider-concept
and stored cards are all things some providers have and others do not, and a
protocol that includes them would be a protocol half its implementations raise
`NotImplementedError` from — which is the mistake ADR-A10 corrected for AI
adapters and there is no reason to repeat here.

**Verification is the provider's job, and it is not optional.** A `verify` that
trusts the posted amount is a shop where the customer writes their own price
tag. Every implementation checks a signature or calls the provider back; the
fake does the former so the calling code is exercised the same way in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from apps.common.errors import DomainError


class PaymentRefused(DomainError):
    """The provider says no, and retrying the same request will not help."""

    status_code = 402
    code = "payment_refused"
    default_message = "That payment could not be taken."


class PaymentUnverified(DomainError):
    """A callback did not prove what it claimed.

    Separate from `PaymentRefused` because the two are opposite problems: one
    is a customer whose payment failed, the other is a request that may not
    have come from the provider at all.
    """

    status_code = 400
    code = "payment_unverified"
    default_message = "That payment confirmation could not be verified."


@dataclass(frozen=True)
class Charge:
    """What the customer is sent to in order to pay."""

    reference: str
    redirect_url: str = ""
    instructions: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Settlement:
    """A verified statement that money moved."""

    reference: str
    amount: Decimal
    currency: str
    succeeded: bool
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PaymentProvider(Protocol):
    """What every provider implements. Two methods, no optional ones."""

    key: str
    label: str

    def start(self, *, invoice: Any) -> Charge:
        """Begin a payment for one invoice."""
        ...

    def verify(self, *, payload: dict[str, Any]) -> Settlement:
        """Turn a callback into a settlement, or raise `PaymentUnverified`."""
        ...


_REGISTRY: dict[str, PaymentProvider] = {}


def register(provider: PaymentProvider) -> PaymentProvider:
    _REGISTRY[provider.key] = provider
    return provider


def get(key: str) -> PaymentProvider:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise PaymentRefused(f"{key!r} is not a payment method this platform offers.") from None


def registry() -> dict[str, PaymentProvider]:
    return dict(_REGISTRY)
