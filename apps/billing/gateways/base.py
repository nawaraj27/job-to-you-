"""
Hosted payment gateways (ADR-A58).

`providers/` answers "is this a settlement?" for money that has already moved.
A hosted gateway needs three more things before that question can be asked —
send the customer somewhere, recognise them when they come back, and ask the
gateway what actually happened — and those three are this interface:

  * **`begin`** — the form or redirect that takes the customer to the gateway.
  * **`parse_return`** — the customer's browser came back with some query
    parameters. Check what can be checked locally (eSewa signs its response;
    Khalti does not) and say which attempt it is about. Nothing is trusted
    beyond the reference: the browser is not the gateway.
  * **`lookup`** — ask the gateway, server to server, what state that
    attempt is in. This is the only answer the outcome is ever taken from.

A gateway is a module with credentials. Adding one is a file here, a block in
`settings.PAYMENT_GATEWAYS`, and its tests — nothing in the checkout service
knows which gateway it is talking to.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import httpx
from django.conf import settings

from apps.common.errors import DomainError

from ..models import AttemptState


class GatewayUnavailable(DomainError):
    """The gateway could not be reached, or answered with something unusable.

    Not a failed payment: the customer may well have paid. The attempt stays
    open and is checked again later.
    """

    status_code = 503
    code = "gateway_unavailable"
    default_message = "The payment service did not answer. We will check again shortly."


class GatewayNotConfigured(DomainError):
    status_code = 400
    code = "gateway_not_configured"
    default_message = "That payment method is not available."


@dataclass(frozen=True)
class Checkout:
    """Where the customer goes to pay.

    `method` is "post" for a gateway that takes a signed form (eSewa) and
    "redirect" for one that hands back a URL (Khalti).
    """

    method: str
    url: str
    fields: dict[str, str] = field(default_factory=dict)
    gateway_token: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReturnTrip:
    """What a browser return proved: which attempt it concerns, nothing more."""

    reference: str
    gateway_token: str = ""
    #: An amount the return carried *and* that was covered by a signature.
    #: Compared against the attempt; never used to settle anything.
    signed_amount: Decimal | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Lookup:
    """The gateway's own answer about one attempt."""

    state: AttemptState
    gateway_status: str
    amount: Decimal | None = None
    gateway_ref: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReturnUrls:
    success: str
    failure: str
    website: str


class Gateway(Protocol):
    key: str
    label: str
    #: Currencies the gateway settles in. Both Nepali wallets are NPR only.
    currencies: frozenset[str]

    def problems(self) -> list[str]:
        """Configuration faults. Empty means usable."""
        ...

    def begin(self, *, attempt: Any, urls: ReturnUrls) -> Checkout: ...

    def parse_return(
        self, *, reference: str, params: dict[str, str], failed: bool
    ) -> ReturnTrip: ...

    def lookup(self, *, attempt: Any) -> Lookup: ...


def config(key: str) -> dict[str, Any]:
    return dict(getattr(settings, "PAYMENT_GATEWAYS", {}).get(key, {}))


def environment() -> str:
    return str(getattr(settings, "PAYMENTS_ENVIRONMENT", "test"))


# --- HTTP ------------------------------------------------------------------
#
# One seam for every outbound call, so a test can answer as the gateway would
# (`httpx.MockTransport`) without patching module internals, and so no test can
# reach a real gateway by accident: the test settings install a transport that
# refuses everything not explicitly answered.

_transport_factory: Callable[[], httpx.BaseTransport | None] = lambda: None  # noqa: E731


def set_transport(factory: Callable[[], httpx.BaseTransport | None]) -> None:
    global _transport_factory
    _transport_factory = factory


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"network blocked in tests: {request.url.host}", request=request)


def client() -> httpx.Client:
    transport = _transport_factory()
    if transport is None and getattr(settings, "PAYMENTS_BLOCK_NETWORK", False):
        transport = httpx.MockTransport(_refuse)
    return httpx.Client(
        timeout=getattr(settings, "PAYMENTS_HTTP_TIMEOUT", 15),
        transport=transport,
        follow_redirects=False,
        headers={"User-Agent": "Kaarya-billing/1"},
    )


_REGISTRY: dict[str, Gateway] = {}


def register(gateway: Gateway) -> Gateway:
    _REGISTRY[gateway.key] = gateway
    return gateway


def get(key: str) -> Gateway:
    gateway = _REGISTRY.get(key)
    if gateway is None:
        raise GatewayNotConfigured()
    return gateway


def registry() -> dict[str, Gateway]:
    return dict(_REGISTRY)


def enabled() -> list[Gateway]:
    """Gateways a customer may be offered: switched on and correctly configured."""
    return [g for g in _REGISTRY.values() if config(g.key).get("enabled") and not g.problems()]
