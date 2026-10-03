"""
Rate limiting (ADR-A41).

Fixed-window counters in the cache. No framework: the whole mechanism is one
atomic increment and a comparison, and the domain limits that matter most
(applications per day, messages per company) are business rules that belong in
services anyway.

Fixed rather than sliding window is a deliberate trade. A boundary-straddling
burst can reach 2× the limit; for "5 logins per 15 minutes" that is 10 attempts
in an instant, which is not a meaningful attack, and a sliding window costs a
sorted set and a cleanup path for no security gain at these numbers.

Two dimensions everywhere
-------------------------
Every sensitive rule is enforced per-identity *and* per-IP. Identity alone lets
an attacker lock a victim out by hammering their email; IP alone is defeated by
any botnet. Identity limits use progressive delay rather than a hard lock, so a
third party can slow a victim but never lock them out.

Failure policy is per-rule, and it is the important decision
------------------------------------------------------------
If the cache is unavailable, `fail_closed` rules refuse (an unauthenticated
login attempt is safe to reject) and open rules allow (taking search down
because a cache is missing is self-inflicted damage).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

from .errors import QuotaExceeded


@dataclass(frozen=True)
class Rule:
    """One limit. `name` namespaces the key so rules cannot collide."""

    name: str
    limit: int
    window_seconds: int
    fail_closed: bool = True

    @property
    def retry_after(self) -> int:
        return self.window_seconds


# Security-sensitive and expensive operations (SRS §15.4).
LOGIN_PER_IDENTITY = Rule("login.identity", limit=5, window_seconds=900)
LOGIN_PER_IP = Rule("login.ip", limit=20, window_seconds=900)
SIGNUP_PER_IP = Rule("signup.ip", limit=5, window_seconds=3600)
#: "You already have an account" emails to one address (D-12). Checked silently.
SIGNUP_NOTICE_PER_IDENTITY = Rule("signup.notice", limit=3, window_seconds=3600)
PASSWORD_RESET_PER_IDENTITY = Rule("reset.identity", limit=3, window_seconds=3600)
PASSWORD_RESET_PER_IP = Rule("reset.ip", limit=10, window_seconds=3600)
UPLOAD_PER_USER = Rule("upload.user", limit=20, window_seconds=3600)
AI_PER_USER = Rule("ai.user", limit=30, window_seconds=3600)
APPLICATION_PER_USER = Rule("application.user", limit=50, window_seconds=86400)
CANDIDATE_SEARCH_PER_MEMBERSHIP = Rule("search.candidates", limit=30, window_seconds=60)

# Messaging (SRS §12). Two rules, because one is not enough: a per-candidate
# limit alone permits a thousand people receiving one message each, which is
# precisely the bulk outreach the pair exists to prevent.
MESSAGE_PER_COMPANY_CANDIDATE = Rule("message.pair", limit=20, window_seconds=86400)
MESSAGE_PER_COMPANY_DAILY = Rule("message.company", limit=200, window_seconds=86400)
WEBHOOK_PER_ENDPOINT = Rule("webhook.endpoint", limit=120, window_seconds=60, fail_closed=False)
JOB_SEARCH_PER_CLIENT = Rule("search.jobs", limit=60, window_seconds=60, fail_closed=False)


class RateLimited(QuotaExceeded):
    code = "rate_limited"
    default_message = "Too many attempts. Try again shortly."

    def __init__(self, message: str | None = None, *, retry_after: int = 60) -> None:
        super().__init__(message, details={"retry_after": retry_after})
        self.retry_after = retry_after


def _key(rule: Rule, identifier: str) -> str:
    # The identifier can be an email or an IP; hashing keeps personal data out
    # of cache keys, which are visible in Redis and in monitoring tools.
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]
    bucket = int(time.time() // rule.window_seconds)
    return f"rl:{rule.name}:{digest}:{bucket}"


def check(rule: Rule, identifier: str, *, consume: bool = True) -> int:
    """Return the remaining allowance, or raise RateLimited.

    `consume=False` inspects without counting — used to show a limit in the UI
    without spending one.
    """
    if not getattr(settings, "RATELIMIT_ENABLED", True):
        return rule.limit

    key = _key(rule, identifier)

    try:
        if not consume:
            used = cache.get(key, 0)
        else:
            # add() then incr() is the atomic pattern: the first caller in a
            # window creates the counter with its TTL, everyone else increments.
            cache.add(key, 0, timeout=rule.window_seconds)
            used = cache.incr(key)
    except Exception:  # noqa: BLE001 - a cache outage must not raise into a view
        if rule.fail_closed:
            raise RateLimited(
                "This action is temporarily unavailable. Try again shortly.",
                retry_after=rule.retry_after,
            ) from None
        return rule.limit

    if used > rule.limit:
        raise RateLimited(retry_after=rule.retry_after)

    return max(rule.limit - used, 0)


def reset(rule: Rule, identifier: str) -> None:
    """Forget this identity's attempts under one rule.

    For the moment a limit's question has been answered in the user's favour.
    `LOGIN_PER_IDENTITY` counts attempts before authentication — it has to, or
    it could not see a failure — but leaving successes on the counter means five
    *correct* passwords in fifteen minutes lock the account out, which is the
    opposite of what a brute-force limit is for. Clearing on success keeps every
    failure counted and stops punishing the person who knows their password.
    """
    if not getattr(settings, "RATELIMIT_ENABLED", True):
        return
    try:
        cache.delete(_key(rule, identifier))
    except Exception:  # noqa: BLE001 - a cache outage must not raise into a view
        return


def enforce(rules: list[tuple[Rule, str]]) -> None:
    """Apply several rules, most specific first.

    Ordered so the identity limit is consumed before the IP limit: a legitimate
    user behind a shared address should hit their own limit, not their office's.
    """
    for rule, identifier in rules:
        check(rule, identifier)


def client_ip(request: HttpRequest) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.META.get("REMOTE_ADDR") or "unknown"
