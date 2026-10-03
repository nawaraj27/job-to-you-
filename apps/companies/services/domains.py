"""
Domain proof for company verification (SRS §19.2 "domain proof, then documents";
§5.2 `EmailAddress` "supports work-email proof for company domain verification").

`CompanyDomain` existed with no service and no screen (Loop Closure EMP-03). The
proof is the one the SRS names: a member who can edit the company verifies an
address at the company's own website domain, and that verified address records
the domain as proven. A reviewer then sees it beside the documents.

Deliberately not a gate on submitting: many small companies here run on a
free-mail address and have no domain of their own. Whether proof should be
required is a product decision, recorded in the loop ledger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState
from apps.core.branding import site_name

from .. import policies
from ..models import CompanyDomain

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import EmailAddress, User

    from ..models import Company, CompanyMembership

#: Addresses anybody can create. A Gmail address proves nothing about a company.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "icloud.com",
        "proton.me",
        "protonmail.com",
        "aol.com",
        "zoho.com",
        "gmx.com",
        "mail.com",
    }
)


def website_domain(company: Company) -> str:
    """The company's domain from its website, without `www.`; "" when there is none."""
    host = (urlsplit(company.website or "").hostname or "").lower().strip(".")
    host = host.removeprefix("www.")
    return "" if not host or host in FREE_MAIL_DOMAINS or "." not in host else host


def proven_domain(company: Company) -> CompanyDomain | None:
    return (
        CompanyDomain.objects.filter(company=company, verified_at__isnull=False)
        .order_by("-verified_at")
        .first()
    )


def _address_matches(email: str, domain: str) -> bool:
    email_domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return bool(domain) and (email_domain == domain or email_domain.endswith("." + domain))


@transaction.atomic
def add_work_email(
    *, actor: User, membership: CompanyMembership, company: Company, email: str
) -> tuple[EmailAddress, str]:
    """Attach an address at the company domain to the actor and mint its verification token."""
    from apps.accounts.models import EmailAddress
    from apps.accounts.services import identity

    policies.require_can_edit_company(membership)
    domain = website_domain(company)
    if not domain:
        raise InvalidState(
            "Add your company website on the profile first; the domain comes from it."
        )
    email = (email or "").strip().lower()
    if not _address_matches(email, domain):
        raise InvalidState(f"Use an address ending in @{domain}.")
    if EmailAddress.objects.filter(email__iexact=email).exclude(user=actor).exists():
        raise Conflict("That address belongs to another account.")

    address, _ = EmailAddress.objects.get_or_create(user=actor, email=email)
    audit.record(
        verb="company.domain_email_added",
        actor=actor,
        target=company,
        metadata={"domain": domain},
    )
    return address, identity.issue_verification_token(address=address)


def verified_address_for(actor: User, company: Company) -> EmailAddress | None:
    """A verified address of the actor's at the company domain, if there is one."""
    from apps.accounts.models import EmailAddress

    domain = website_domain(company)
    if not domain:
        return None
    for address in EmailAddress.objects.filter(user=actor, verified_at__isnull=False):
        if _address_matches(address.email, domain):
            return address
    return None


@transaction.atomic
def prove_domain(*, actor: User, membership: CompanyMembership, company: Company) -> CompanyDomain:
    """Record the company domain as proven by the actor's verified work address."""
    policies.require_can_edit_company(membership)
    domain = website_domain(company)
    address = verified_address_for(actor, company)
    if address is None:
        raise InvalidState(
            f"Verify an address ending in @{domain} first."
            if domain
            else "Add your company website on the profile first."
        )

    record, _ = CompanyDomain.objects.get_or_create(company=company, domain=domain)
    if record.verified_at is None:
        try:
            with transaction.atomic():
                record.verified_at = timezone.now()
                record.save(update_fields=["verified_at"])
        except IntegrityError:
            raise Conflict(
                f"Another company has already proven {domain}. Contact {site_name()} if that is wrong."
            ) from None
        audit.record(
            verb="company.domain_proven",
            actor=actor,
            target=company,
            metadata={"domain": domain, "by": address.email},
        )
    return record
