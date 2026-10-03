"""
Outbound webhooks (SRS §5.12, §15.4).

A webhook URL is a **user-supplied URL that this server fetches**, which makes
it the same class of hazard as link checking: without an allow-list it is an
SSRF primitive pointed at the cloud metadata endpoint, at localhost, and at
everything else inside the network perimeter. So the destination is validated
at registration *and* again at delivery — registration alone is not enough,
because DNS can be repointed afterwards.

Signing is HMAC-SHA256 over `timestamp.body`. The timestamp is inside the
signed material, so a captured request cannot be replayed a week later against
a receiver that checks freshness.

The secret is **encrypted, not hashed** (ADR-A45). A password is verified and
so can be hashed; a signing key is used on every outbound request, and hashing
it would leave nothing to sign with — the header would be decoration, and a
receiver checking it would be checking nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState, NotFound
from apps.common.ids import as_uuid

from ..models import (
    Channel,
    DeliveryStatus,
    NotificationDelivery,
    WebhookDelivery,
    WebhookEndpoint,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company

    from ..models import OutboxEvent

logger = logging.getLogger(__name__)

#: How long the receiver has. Short: a slow endpoint must not hold a worker.
TIMEOUT_SECONDS = 5

#: Signature freshness the receiver should enforce. Documented here because it
#: is part of the contract even though we do not check it ourselves.
SIGNATURE_TOLERANCE_SECONDS = 300

MAX_ATTEMPTS = 6
MAX_ENDPOINTS_PER_COMPANY = 5

#: Consecutive failures before an endpoint is switched off. An endpoint dead
#: for this long is not coming back on its own, and retrying it forever is a
#: standing outbound request to somebody else's server.
DISABLE_AFTER_FAILURES = 20


class UnsafeWebhookUrl(InvalidState):
    """The destination is not somewhere this server may be made to fetch."""


# --- registration ---------------------------------------------------------


@transaction.atomic
def create_endpoint(
    *,
    company: Company,
    actor: User,
    url: str,
    event_types: list[str] | None = None,
    description: str = "",
) -> tuple[WebhookEndpoint, str]:
    """Register a destination. Returns the endpoint and its signing secret."""
    require_safe_url(url)

    live = WebhookEndpoint.objects.filter(company=company, is_active=True).count()
    if live >= MAX_ENDPOINTS_PER_COMPANY:
        raise InvalidState(f"A company may keep up to {MAX_ENDPOINTS_PER_COMPANY} endpoints.")

    secret = secrets.token_urlsafe(32)
    endpoint = WebhookEndpoint.objects.create(
        company=company,
        url=url.strip(),
        secret=secret,
        event_types=_clean_types(event_types),
        description=description.strip()[:200],
        created_by=actor,
    )
    audit.record(
        verb="webhook.endpoint_created",
        actor=actor,
        company=company,
        metadata={"url": endpoint.url},
    )
    return endpoint, secret


@transaction.atomic
def rotate_secret(*, endpoint: WebhookEndpoint, actor: User) -> str:
    secret = secrets.token_urlsafe(32)
    endpoint.secret = secret
    endpoint.save(update_fields=["secret", "updated_at"])

    audit.record(
        verb="webhook.secret_rotated",
        actor=actor,
        company=endpoint.company,
        metadata={"url": endpoint.url},
    )
    return secret


@transaction.atomic
def set_active(*, endpoint: WebhookEndpoint, actor: User, active: bool) -> WebhookEndpoint:
    endpoint.is_active = active
    endpoint.disabled_at = None if active else timezone.now()
    endpoint.disabled_reason = "" if active else "Switched off by the company."
    if active:
        endpoint.consecutive_failures = 0
    endpoint.save(
        update_fields=[
            "is_active",
            "disabled_at",
            "disabled_reason",
            "consecutive_failures",
            "updated_at",
        ]
    )
    return endpoint


@transaction.atomic
def delete_endpoint(*, endpoint: WebhookEndpoint, actor: User) -> None:
    company, url = endpoint.company, endpoint.url
    endpoint.delete()
    audit.record(
        verb="webhook.endpoint_deleted", actor=actor, company=company, metadata={"url": url}
    )


def _clean_types(event_types: list[str] | None) -> list[str]:
    """Only events that exist. A subscription to a typo is silence."""
    from ..events import registry

    known = set(registry())
    return [item for item in (event_types or []) if item in known]


# --- SSRF guard (SRS §15.4) ----------------------------------------------


def require_safe_url(url: str) -> str:
    """Refuse anything this server must not be made to fetch.

    Checked at registration and again at delivery: a hostname that resolved to
    a public address last week can resolve to 169.254.169.254 today, and the
    check that matters is the one nearest the request.
    """
    parsed = urlparse((url or "").strip())

    if parsed.scheme != "https" and not settings.DEBUG:
        raise UnsafeWebhookUrl("A webhook URL must be https.")
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeWebhookUrl("A webhook URL must be http or https.")
    if not parsed.hostname:
        raise UnsafeWebhookUrl("That URL has no host.")
    if parsed.port is not None and parsed.port not in {80, 443, 8000, 8443}:
        raise UnsafeWebhookUrl("That port is not one this platform will call.")

    for address in _resolve(parsed.hostname):
        if not address.is_global or address.is_multicast:
            raise UnsafeWebhookUrl(
                "That address is inside a private network. A webhook must point at "
                "a public endpoint."
            )
    return url.strip()


def _resolve(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address the name resolves to.

    All of them, not the first: a name with one public and one private answer
    is the standard DNS-rebinding shape, and checking only the first would pass
    it.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeWebhookUrl("That host does not resolve.") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:  # pragma: no cover - getaddrinfo returned a non-address
            continue
    if not addresses:
        raise UnsafeWebhookUrl("That host does not resolve.")
    return addresses


# --- signing --------------------------------------------------------------


def sign(*, secret: str, timestamp: int, body: bytes) -> str:
    """`sha256=<hex>` over `timestamp.body`.

    The timestamp is inside the signed material, so a captured request cannot
    be replayed later against a receiver that checks freshness.
    """
    material = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --- delivery -------------------------------------------------------------


def endpoints_for(event: OutboxEvent) -> list[WebhookEndpoint]:
    """Which live endpoints want this event.

    Scoped to the company in the payload: an event with no company reaches no
    endpoint, which is the safe direction for a missing field.
    """
    company_id = event.payload.get("company_id")
    if not company_id:
        return []

    return [
        endpoint
        for endpoint in WebhookEndpoint.objects.filter(
            company_id=company_id, is_active=True, disabled_at__isnull=True
        )
        if endpoint.wants(event.type)
    ]


def dispatch(event: OutboxEvent) -> int:
    """Queue this event to every endpoint that wants it.

    The delivery ledger claims each (event, endpoint) pair, so a re-run of the
    outbox consumer cannot send the same event to one endpoint twice.
    """
    from django.db import IntegrityError

    queued = 0
    for endpoint in endpoints_for(event):
        try:
            with transaction.atomic():
                NotificationDelivery.objects.create(
                    event=event,
                    endpoint=endpoint,
                    channel=Channel.WEBHOOK,
                    status=DeliveryStatus.PENDING,
                )
        except IntegrityError:
            continue

        WebhookDelivery.objects.create(endpoint=endpoint, event=event, next_retry_at=timezone.now())
        queued += 1
    return queued


def deliver_due(*, limit: int = 100) -> dict[str, int]:
    """Attempt every delivery whose time has come."""
    due = (
        WebhookDelivery.objects.filter(
            delivered_at__isnull=True,
            next_retry_at__isnull=False,
            next_retry_at__lte=timezone.now(),
            attempt__lt=MAX_ATTEMPTS,
        )
        .select_related("endpoint", "event")
        .order_by("next_retry_at")[:limit]
    )

    sent = failed = 0
    for delivery in due:
        if attempt(delivery):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}


def attempt(delivery: WebhookDelivery) -> bool:
    """One HTTP POST, recorded either way."""
    import requests

    endpoint = delivery.endpoint
    delivery.attempt += 1

    body = json.dumps(
        {
            "id": delivery.event_id,
            "type": delivery.event.type,
            "schema_version": delivery.event.schema_version,
            "created_at": delivery.event.created_at.isoformat(),
            "data": delivery.event.payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    try:
        # Re-validated at the moment of the request: the address behind a
        # hostname can change after registration.
        require_safe_url(endpoint.url)
        timestamp = int(timezone.now().timestamp())
        response = requests.post(
            endpoint.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-CareerAI-Event": delivery.event.type,
                "X-CareerAI-Timestamp": str(timestamp),
                # Signed with the plaintext secret the company holds. We store
                # only its hash, so the signature is computed from the value
                # they were given at creation — see `verify_secret`.
                "X-CareerAI-Signature": sign(
                    secret=endpoint.secret, timestamp=timestamp, body=body
                ),
                "User-Agent": "CareerAI-Webhooks/1",
            },
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except UnsafeWebhookUrl as exc:
        _record_failure(delivery, error=exc.message, disable=True)
        return False
    except Exception as exc:  # noqa: BLE001 - any transport failure is a retry
        _record_failure(delivery, error=str(exc)[:300])
        return False

    delivery.status_code = response.status_code
    delivery.response_excerpt = (response.text or "")[:500]

    if 200 <= response.status_code < 300:
        delivery.delivered_at = timezone.now()
        delivery.next_retry_at = None
        delivery.save(
            update_fields=[
                "attempt",
                "status_code",
                "response_excerpt",
                "delivered_at",
                "next_retry_at",
            ]
        )
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=0)
        NotificationDelivery.objects.filter(
            event=delivery.event, endpoint=endpoint, channel=Channel.WEBHOOK
        ).update(status=DeliveryStatus.SENT)
        return True

    _record_failure(delivery, error=f"HTTP {response.status_code}")
    return False


def _record_failure(delivery: WebhookDelivery, *, error: str, disable: bool = False) -> None:
    """Back off, and eventually stop.

    Exponential, capped by `MAX_ATTEMPTS`. An endpoint that fails constantly is
    switched off rather than retried forever: retrying forever is a standing
    outbound request against somebody else's server, which is our problem to
    stop making.
    """
    import datetime as dt

    delivery.error = error[:300]
    delivery.next_retry_at = (
        None
        if delivery.attempt >= MAX_ATTEMPTS
        else timezone.now() + dt.timedelta(seconds=60 * (2**delivery.attempt))
    )
    delivery.save(
        update_fields=["attempt", "error", "next_retry_at", "status_code", "response_excerpt"]
    )

    endpoint = delivery.endpoint
    WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
        consecutive_failures=endpoint.consecutive_failures + 1
    )
    endpoint.refresh_from_db()

    if disable or endpoint.consecutive_failures >= DISABLE_AFTER_FAILURES:
        endpoint.is_active = False
        endpoint.disabled_at = timezone.now()
        endpoint.disabled_reason = error[:200]
        endpoint.save(update_fields=["is_active", "disabled_at", "disabled_reason", "updated_at"])

    if delivery.next_retry_at is None:
        NotificationDelivery.objects.filter(
            event=delivery.event, endpoint=endpoint, channel=Channel.WEBHOOK
        ).update(status=DeliveryStatus.FAILED, reason=error[:120])


def verify_signature(
    *, endpoint: WebhookEndpoint, timestamp: int, body: bytes, signature: str
) -> bool:
    """What a receiver does, implemented once so the tests can prove it works.

    Constant-time, because a byte-by-byte comparison of a signature leaks the
    signature one byte at a time.
    """
    expected = sign(secret=endpoint.secret, timestamp=timestamp, body=body)
    return hmac.compare_digest(expected, signature)


def endpoint_for_company(company: Company, public_id: str) -> WebhookEndpoint:
    """Scoped lookup. An id from another company resolves to nothing."""
    if as_uuid(public_id) is None:
        raise NotFound("That endpoint does not exist, or you cannot see it.")
    endpoint = WebhookEndpoint.objects.filter(company=company, public_id=public_id).first()
    if endpoint is None:
        raise NotFound("That endpoint does not exist, or you cannot see it.")
    return endpoint
