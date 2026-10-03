"""
eSewa ePay v2 (developer.esewa.com.np, "Epay").

The flow, as eSewa documents it:

  1. The merchant posts a form to `/api/epay/main/v2/form` carrying the amount,
     a `transaction_uuid` of its own, the `product_code`, the success and
     failure URLs and an HMAC-SHA256 signature (base64) over
     `total_amount,transaction_uuid,product_code`.
  2. The customer logs in on eSewa's page and pays.
  3. eSewa redirects the browser to `success_url?data=<base64 JSON>`. The JSON
     carries its own `signed_field_names` and `signature`, which are checked
     here with the same secret.
  4. The merchant confirms with the status API
     (`/api/epay/transaction/status/?product_code&total_amount&transaction_uuid`),
     which answers COMPLETE, PENDING, FULL_REFUND, PARTIAL_REFUND, AMBIGUOUS,
     NOT_FOUND or CANCELED.

Step 3 proves the browser carries something eSewa signed. Step 4 is what the
outcome is taken from. A signed success that the status API does not confirm
is not a payment.

eSewa's documented ePay v2 has no refund API: a refund is made from the
merchant portal and shows up here as FULL_REFUND or PARTIAL_REFUND the next
time the attempt is looked up.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
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

#: The fields eSewa signs on the request, in the order it requires.
REQUEST_SIGNED_FIELDS = ("total_amount", "transaction_uuid", "product_code")
#: A response signature that does not cover these has not vouched for the
#: things this module reads from it.
RESPONSE_MUST_SIGN = frozenset({"transaction_uuid", "total_amount", "status", "product_code"})

STATUS_MAP: dict[str, AttemptState] = {
    "COMPLETE": AttemptState.SUCCEEDED,
    "PENDING": AttemptState.PENDING,
    "AMBIGUOUS": AttemptState.PENDING,
    "FULL_REFUND": AttemptState.REFUNDED,
    "PARTIAL_REFUND": AttemptState.REFUNDED,
    "CANCELED": AttemptState.CANCELLED,
    # Not found means "never paid" — until it is old enough, it may simply be
    # a customer still on eSewa's page. The service decides which.
    "NOT_FOUND": AttemptState.INITIATED,
}


def format_amount(amount: Decimal) -> str:
    """`1200.00` -> "1200", `1200.50` -> "1200.5". The same string is signed
    and sent, so the two can never disagree."""
    text = f"{amount.normalize():f}"
    return text


def parse_amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise PaymentUnverified(
            "The payment response carried an amount that is not a number."
        ) from None


def sign(message: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def signed_message(values: dict[str, Any], names: list[str] | tuple[str, ...]) -> str:
    return ",".join(f"{name}={values.get(name, '')}" for name in names)


class EsewaGateway:
    key = "esewa"
    label = "eSewa"
    currencies = frozenset({"NPR"})

    def _config(self) -> dict[str, Any]:
        return config(self.key)

    def problems(self) -> list[str]:
        cfg = self._config()
        missing = [
            name
            for name, setting in (
                ("ESEWA_PRODUCT_CODE", "product_code"),
                ("ESEWA_SECRET_KEY", "secret_key"),
                ("ESEWA_FORM_URL", "form_url"),
                ("ESEWA_STATUS_URL", "status_url"),
            )
            if not cfg.get(setting)
        ]
        problems = [f"{name} is not set." for name in missing]
        for setting in ("form_url", "status_url"):
            url = str(cfg.get(setting, ""))
            if url and not url.startswith("https://"):
                problems.append(f"eSewa {setting} must be an https URL.")
        return problems

    # --- 1. the form ---------------------------------------------------------

    def begin(self, *, attempt: Any, urls: ReturnUrls) -> Checkout:
        cfg = self._config()
        total = format_amount(attempt.amount)
        fields = {
            "amount": total,
            "tax_amount": "0",
            "total_amount": total,
            "transaction_uuid": attempt.reference,
            "product_code": cfg["product_code"],
            "product_service_charge": "0",
            "product_delivery_charge": "0",
            "success_url": urls.success,
            "failure_url": urls.failure,
            "signed_field_names": ",".join(REQUEST_SIGNED_FIELDS),
        }
        fields["signature"] = sign(signed_message(fields, REQUEST_SIGNED_FIELDS), cfg["secret_key"])
        return Checkout(method="post", url=cfg["form_url"], fields=fields)

    # --- 3. the browser comes back ---------------------------------------------

    def parse_return(self, *, reference: str, params: dict[str, str], failed: bool) -> ReturnTrip:
        raw = params.get("data", "")
        if failed and not raw:
            # eSewa's failure redirect carries nothing to verify. The reference
            # came from our own URL; the status API says what happened.
            return ReturnTrip(reference=reference)
        if not raw:
            raise PaymentUnverified("The payment response was empty.")

        body = self.decode(raw)
        names = [n.strip() for n in str(body.get("signed_field_names", "")).split(",") if n.strip()]
        if not RESPONSE_MUST_SIGN.issubset(names):
            raise PaymentUnverified(
                "The payment response was not signed over the fields it must be."
            )
        expected = sign(signed_message(body, names), self._config()["secret_key"])
        if not hmac.compare_digest(expected, str(body.get("signature", ""))):
            raise PaymentUnverified("The payment response did not carry a valid signature.")

        if str(body.get("transaction_uuid", "")) != reference:
            raise PaymentUnverified("The payment response is for a different payment.")
        if str(body.get("product_code", "")) != self._config()["product_code"]:
            raise PaymentUnverified("The payment response is for a different merchant.")

        return ReturnTrip(
            reference=reference,
            signed_amount=parse_amount(body.get("total_amount", "")),
            detail={
                "return_status": str(body.get("status", "")),
                "transaction_code": str(body.get("transaction_code", "")),
            },
        )

    @staticmethod
    def decode(raw: str) -> dict[str, Any]:
        try:
            padded = raw + "=" * (-len(raw) % 4)
            text = base64.b64decode(padded, validate=False).decode("utf-8")
            # Numbers are kept as the literal text eSewa signed: re-formatting
            # 1000.0 as "1000" would break a signature that was correct.
            body = json.loads(text, parse_float=str, parse_int=str)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise PaymentUnverified("The payment response could not be read.") from None
        if not isinstance(body, dict):
            raise PaymentUnverified("The payment response could not be read.")
        return body

    # --- 4. ask eSewa ----------------------------------------------------------

    def lookup(self, *, attempt: Any) -> Lookup:
        cfg = self._config()
        params = {
            "product_code": cfg["product_code"],
            "total_amount": format_amount(attempt.amount),
            "transaction_uuid": attempt.reference,
        }
        try:
            with http_client() as http:
                response = http.get(cfg["status_url"], params=params)
        except httpx.HTTPError as error:
            raise GatewayUnavailable(
                f"eSewa status check failed: {type(error).__name__}."
            ) from None
        if response.status_code >= 500:
            raise GatewayUnavailable(f"eSewa status check answered {response.status_code}.")
        try:
            body = response.json()
        except ValueError:
            raise GatewayUnavailable("eSewa status check did not answer with JSON.") from None
        if not isinstance(body, dict):
            raise GatewayUnavailable("eSewa status check answered with something unexpected.")

        status = str(body.get("status", "")).upper()
        if response.status_code != 200 or status not in STATUS_MAP:
            # A 4xx carries {"code", "error_message"}: eSewa does not know this
            # transaction as asked. That is NOT_FOUND by another name.
            if response.status_code in (400, 404) and not status:
                status = "NOT_FOUND"
            else:
                raise GatewayUnavailable(f"eSewa status check answered {response.status_code}.")

        if status != "NOT_FOUND":
            if str(body.get("transaction_uuid", attempt.reference)) != attempt.reference:
                raise GatewayUnavailable("eSewa answered about a different transaction.")
            if str(body.get("product_code", cfg["product_code"])) != cfg["product_code"]:
                raise GatewayUnavailable("eSewa answered about a different merchant.")

        amount = None
        if body.get("total_amount") not in (None, ""):
            amount = parse_amount(body["total_amount"])
        ref_id = str(body.get("ref_id") or "")
        return Lookup(
            state=STATUS_MAP[status],
            gateway_status=status,
            amount=amount,
            gateway_ref="" if ref_id in ("None", "null") else ref_id,
            detail={"status_response": _safe(body)},
        )


def _safe(body: dict[str, Any]) -> dict[str, Any]:
    """The gateway's answer as stored: never a key, never a signature."""
    return {k: v for k, v in body.items() if k not in {"signature", "secret_key"}}


gateway = register(EsewaGateway())
