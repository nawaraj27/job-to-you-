"""
Khalti web checkout, KPG-2 (docs.khalti.com, "Web Checkout (KPG-2)").

  1. The merchant calls `POST epayment/initiate/` from the server with its
     secret key, the amount **in paisa** (minimum 1000, i.e. Rs 10), its own
     `purchase_order_id`, and the return URL. Khalti answers with a `pidx` and a
     `payment_url`.
  2. The customer pays on Khalti's page and is sent back to the return URL
     with `pidx`, `status`, `purchase_order_id` and friends in the query.
  3. The merchant calls `POST epayment/lookup/` with the `pidx`. Khalti's own
     rule: only `Completed` is a payment.

The return in step 2 is unsigned, so nothing in it is believed except which
attempt it concerns, and even that is cross-checked: the `pidx` must be the one
Khalti issued for that attempt.

The sandbox key is per merchant: Khalti issues it after signing up at
test-admin.khalti.com. There is no published shared key, so until one is put in
`KHALTI_SECRET_KEY` this gateway reports itself unconfigured and is not offered.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from apps.billing.providers.base import PaymentUnverified

from ..models import AttemptState
from .base import (
    Checkout,
    GatewayUnavailable,
    Lookup,
    ReturnTrip,
    ReturnUrls,
    config,
    register,
)
from .base import client as http_client

#: Khalti's floor, in paisa.
MINIMUM_PAISA = 1000

STATUS_MAP: dict[str, AttemptState] = {
    "Completed": AttemptState.SUCCEEDED,
    "Pending": AttemptState.PENDING,
    "Initiated": AttemptState.INITIATED,
    "Refunded": AttemptState.REFUNDED,
    "Partially Refunded": AttemptState.REFUNDED,
    "Expired": AttemptState.EXPIRED,
    "User canceled": AttemptState.CANCELLED,
}


def to_paisa(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def from_paisa(value: Any) -> Decimal:
    return (Decimal(str(value)) / 100).quantize(Decimal("0.01"))


class KhaltiGateway:
    key = "khalti"
    label = "Khalti"
    currencies = frozenset({"NPR"})

    def _config(self) -> dict[str, Any]:
        return config(self.key)

    def problems(self) -> list[str]:
        cfg = self._config()
        problems = []
        if not cfg.get("secret_key"):
            problems.append(
                "KHALTI_SECRET_KEY is not set (a sandbox key is issued per merchant at "
                "test-admin.khalti.com)."
            )
        base_url = str(cfg.get("base_url", ""))
        if not base_url.startswith("https://"):
            problems.append("KHALTI_BASE_URL must be an https URL.")
        return problems

    def _url(self, path: str) -> str:
        return self._config()["base_url"].rstrip("/") + "/" + path

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self._config()['secret_key']}"}

    def begin(self, *, attempt: Any, urls: ReturnUrls) -> Checkout:
        paisa = to_paisa(attempt.amount)
        if paisa < MINIMUM_PAISA:
            raise PaymentUnverified("Khalti takes payments of Rs 10 or more.")
        body = {
            "return_url": urls.success,
            "website_url": urls.website,
            "amount": paisa,
            "purchase_order_id": attempt.reference,
            "purchase_order_name": f"Invoice {attempt.invoice.number}",
        }
        try:
            with http_client() as http:
                response = http.post(
                    self._url("epayment/initiate/"), json=body, headers=self._headers()
                )
        except httpx.HTTPError as error:
            raise GatewayUnavailable(f"Khalti did not answer: {type(error).__name__}.") from None
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if response.status_code != 200 or not answer.get("pidx") or not answer.get("payment_url"):
            raise GatewayUnavailable(
                f"Khalti refused to start the payment ({response.status_code})."
            )
        payment_url = str(answer["payment_url"])
        if not payment_url.startswith("https://"):
            raise GatewayUnavailable("Khalti answered with an unexpected payment link.")
        return Checkout(
            method="redirect",
            url=payment_url,
            gateway_token=str(answer["pidx"]),
            detail={
                "expires_at": answer.get("expires_at", ""),
                "expires_in": answer.get("expires_in"),
            },
        )

    def parse_return(self, *, reference: str, params: dict[str, str], failed: bool) -> ReturnTrip:
        pidx = params.get("pidx", "")
        if not pidx:
            raise PaymentUnverified("The payment response did not say which payment it was.")
        order = params.get("purchase_order_id", "")
        if order and order != reference:
            raise PaymentUnverified("The payment response is for a different payment.")
        return ReturnTrip(
            reference=reference,
            gateway_token=pidx,
            detail={"return_status": params.get("status", "")},
        )

    def lookup(self, *, attempt: Any) -> Lookup:
        if not attempt.gateway_token:
            # Initiation never succeeded, so Khalti has nothing to look up.
            return Lookup(state=AttemptState.INITIATED, gateway_status="NOT_STARTED")
        try:
            with http_client() as http:
                response = http.post(
                    self._url("epayment/lookup/"),
                    json={"pidx": attempt.gateway_token},
                    headers=self._headers(),
                )
        except httpx.HTTPError as error:
            raise GatewayUnavailable(f"Khalti lookup failed: {type(error).__name__}.") from None
        try:
            body = response.json()
        except ValueError:
            raise GatewayUnavailable("Khalti lookup did not answer with JSON.") from None
        if not isinstance(body, dict):
            raise GatewayUnavailable("Khalti lookup answered with something unexpected.")

        status = str(body.get("status", ""))
        if response.status_code == 404 or (response.status_code == 400 and not status):
            return Lookup(
                state=AttemptState.INITIATED, gateway_status="NOT_FOUND", detail={"lookup": body}
            )
        if response.status_code != 200 or status not in STATUS_MAP:
            raise GatewayUnavailable(f"Khalti lookup answered {response.status_code}.")
        if str(body.get("pidx", attempt.gateway_token)) != attempt.gateway_token:
            raise GatewayUnavailable("Khalti answered about a different payment.")

        amount = from_paisa(body["total_amount"]) if body.get("total_amount") is not None else None
        return Lookup(
            state=STATUS_MAP[status],
            gateway_status=status,
            amount=amount,
            gateway_ref=str(body.get("transaction_id") or ""),
            detail={"lookup": body},
        )


gateway = register(KhaltiGateway())
