"""
Expected refusals, told as refusals rather than as breakage.

A `DomainError` already means "the system worked and the answer is no"
(`errors.py`). What was missing was the second half: saying so in a way that
does not look like the site has fallen over. A free candidate who reached
their monthly analysis got, on an htmx action, a red box reading "The
connection dropped or the server didn't answer" — the client's generic
network-failure message, because the refusal came back as a 402 and htmx does
not swap error statuses. On a full page they got one sentence of plain text
with no header, no navigation and no way on.

This module decides, in one place, which refusals are product restrictions
and what the reader can do about each:

  * **Restrictions** — not included in the plan, not permitted for this
    account or role, not available in the current state. Told with a title,
    the service's own sentence and, where there is one, the next step.
  * **Failures** — an external provider down (`ProviderError`), and anything
    that is not a `DomainError` at all. These are *not* described here and
    keep reaching the error handlers and the tracker, so a real defect never
    gets dressed up as a polite "not available".

Authorisation is not decided here. Every refusal was already made by a
service or policy; this only chooses the words and the link.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse

from .errors import (
    Conflict,
    DomainError,
    InvalidState,
    NotAllowed,
    ProviderError,
    QuotaExceeded,
)


@dataclass(frozen=True)
class Restriction:
    """What the reader is told about one refusal."""

    title: str
    message: str
    status: int
    action_label: str = ""
    action_url: str = ""
    #: A second sentence when there is no link to give — who can fix it.
    hint: str = ""
    #: A second, quieter way on: "View plans" beside "Upgrade plan" (ERR-01).
    secondary_label: str = ""
    secondary_url: str = ""
    #: "plan" | "permission" | "state" | "limit" | "input" — for templates
    #: and tests; not shown.
    kind: str = "state"


def describe(exception: DomainError, request: HttpRequest) -> Restriction | None:
    """The restriction this refusal is, or None if it is a failure.

    Order matters: `PlanLimitExceeded` is a `QuotaExceeded`, and a plan
    refusal has a next step a rate limit does not.
    """
    from apps.billing.services.entitlements import PlanLimitExceeded

    if isinstance(exception, ProviderError):
        return None

    if isinstance(exception, PlanLimitExceeded):
        label, url, hint = _plan_action(request)
        # A metered allowance used up is not "not included": the feature is
        # on the plan and comes back on its reset date (ADR-A59).
        metered = "resets_on" in (getattr(exception, "details", None) or {})
        return Restriction(
            title="You've reached your plan's limit" if metered else "Not included in your plan",
            message=exception.message,
            status=exception.status_code,
            action_label=label,
            action_url=url,
            hint=hint,
            # Every plan and what it includes, public — whoever may or may not
            # change the plan can see what the upgrade would give them.
            secondary_label="View plans",
            secondary_url=_url("core:pricing"),
            kind="plan",
        )
    if isinstance(exception, QuotaExceeded):
        return Restriction(
            title="Limit reached",
            message=exception.message,
            status=exception.status_code,
            kind="limit",
        )
    if isinstance(exception, NotAllowed):
        return Restriction(
            title="Not available to your account",
            message=exception.message,
            status=exception.status_code,
            # Not repeated when the service already said who to ask.
            hint="" if "owner" in exception.message.lower() else _permission_hint(request),
            kind="permission",
        )
    if isinstance(exception, InvalidState | Conflict):
        return Restriction(
            title="Not available right now",
            message=exception.message,
            status=exception.status_code,
            kind="state",
        )
    # A bare DomainError: malformed input, most often. Still the reader's
    # answer rather than the site's failure.
    return Restriction(
        title="That could not be done",
        message=exception.message,
        status=exception.status_code,
        kind="input",
    )


def _plan_action(request: HttpRequest) -> tuple[str, str, str]:
    """Where a plan refusal sends somebody, by who is asking: (label, url, hint).

    An employer's plan belongs to the company (decision D-11):

      * the Owner, who holds `billing.manage`, is sent to billing to change it;
      * a Company Admin can open billing and see the plan but not change it,
        so they are sent there to see it and told the Owner changes it;
      * anybody else cannot open billing at all, and is told who can.

    A candidate's plan is their own, and "View plans" is where they change it.
    """
    company = getattr(request, "company", None)
    if company is not None:
        membership = getattr(request, "membership", None)
        grants = getattr(membership, "grants", ()) if membership is not None else ()
        from apps.companies.permissions import Grant
        from apps.companies.policies import can_open_section

        billing = _url("employer:billing", company.slug)
        if Grant.BILLING_MANAGE in grants:
            return "Go to billing", billing, "Change the plan there to include this."
        if can_open_section(membership, "billing"):
            return (
                "See the plan",
                billing,
                f"Only the owner of {company.name} can change the plan.",
            )
        return "", "", f"Only the owner of {company.name} can change the plan. Ask them."
    return "Upgrade plan", _url("dashboard:plan"), ""


def _permission_hint(request: HttpRequest) -> str:
    """Who can do something about a permission refusal, when anyone can.

    Inside a company the answer is a person: roles are set by the Owner and
    Company Admins (`companies.policies.can_manage_team`). The hint names
    them rather than linking to the team screen, which the reader may not be
    allowed to open either.
    """
    company = getattr(request, "company", None)
    membership = getattr(request, "membership", None)
    if company is None or membership is None:
        return ""
    from apps.companies.policies import can_manage_team

    if can_manage_team(membership):
        return ""
    return f"Your role at {company.name} does not include this. An owner or admin can change your role."


def _url(name: str, *args: str) -> str:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def flash_context(request: HttpRequest, error: object) -> dict[str, object]:
    """What an inline flash partial needs, from a message or a refusal.

    Views that catch a `DomainError` and re-render their own region used to
    pass only `exc.message`, and every partial showed it as a red "That
    didn't work" — a plan limit and a crash looked the same. Passing the
    exception instead lets the partial tell them apart: a restriction renders
    the warning component with its next step; anything else stays an error.
    """
    if isinstance(error, DomainError):
        restriction = describe(error, request)
        if restriction is not None:
            return {"flash_error": None, "flash_restriction": restriction}
        return {"flash_error": error.message, "flash_restriction": None}
    return {"flash_error": error or None, "flash_restriction": None}


def flash(request: HttpRequest, error: DomainError) -> None:
    """A refusal as a flash message on the next page (RES-04).

    Views that catch a `DomainError` and redirect used to `messages.error` it,
    so a Viewer opening "New job" landed on the job list under a red
    `role="alert"` box — the same treatment as a crash — for "your role can
    read postings but not create them". A restriction is now a warning, with
    who can help when there is someone; a real failure is still an error.
    """
    from django.contrib import messages

    restriction = describe(error, request)
    if restriction is None:
        messages.error(request, error.message)
        return
    text = " ".join(str(part) for part in (restriction.message, restriction.hint) if part)
    messages.warning(request, text)
