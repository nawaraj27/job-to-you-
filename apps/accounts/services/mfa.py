"""
Multi-factor authentication (ADR-A42).

Built on `django-otp` rather than an in-tree RFC 6238 implementation.
Authentication code benefits more from community review than the project
benefits from one fewer dependency, and django-otp brings three things we would
otherwise write and maintain: the TOTP device model with drift tolerance and
replay protection, the static-token plugin for recovery codes, and the
`OTPMiddleware` that makes `request.user.is_verified()` meaningful.

Two rules the library does not enforce for us:

  * A device is not usable until it is **confirmed** by entering a code, so a
    half-finished enrolment can never lock anyone out.
  * Recovery codes are issued once, shown once, and stored hashed by the plugin.
    Regenerating replaces the whole set, because a partially-known set is worse
    than a fresh one.
"""

from __future__ import annotations

import secrets

from django.db import transaction
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotAllowed

from ..models import User

TOTP_DEVICE_NAME = "authenticator"
RECOVERY_DEVICE_NAME = "recovery"
RECOVERY_CODE_COUNT = 10


def totp_device(user: User, *, confirmed: bool | None = None) -> TOTPDevice | None:
    devices = TOTPDevice.objects.filter(user=user, name=TOTP_DEVICE_NAME)
    if confirmed is not None:
        devices = devices.filter(confirmed=confirmed)
    return devices.first()


def is_enabled(user: User) -> bool:
    return totp_device(user, confirmed=True) is not None


@transaction.atomic
def begin_enrolment(*, user: User) -> TOTPDevice:
    """Create an unconfirmed device and return it with its provisioning URI.

    Restarting enrolment discards the previous unconfirmed device rather than
    accumulating them: a user who abandons the flow and returns should not be
    offered a secret they no longer have in their authenticator.
    """
    if is_enabled(user):
        raise Conflict("Two-factor authentication is already enabled.")

    TOTPDevice.objects.filter(user=user, name=TOTP_DEVICE_NAME, confirmed=False).delete()
    device = TOTPDevice.objects.create(user=user, name=TOTP_DEVICE_NAME, confirmed=False)

    audit.record(verb="account.mfa_enrolment_started", actor=user, target=user)
    return device


@transaction.atomic
def confirm_enrolment(*, user: User, code: str) -> list[str]:
    """Confirm the device with a code, and issue recovery codes.

    Returns the recovery codes in plain text exactly once. They are stored
    hashed by the plugin and cannot be shown again.
    """
    device = totp_device(user, confirmed=False)
    if device is None:
        raise InvalidState("Start setting up two-factor authentication first.")

    if not device.verify_token(code):
        raise NotAllowed("That code is not correct. Check your authenticator and try again.")

    device.confirmed = True
    device.save(update_fields=["confirmed"])

    codes = _issue_recovery_codes(user)
    audit.record(verb="account.mfa_enabled", actor=user, target=user)
    return codes


@transaction.atomic
def disable(*, user: User, code: str) -> None:
    """Turn MFA off, proving possession first.

    Requiring a code means a stolen session cannot quietly remove the second
    factor — which is the first thing an attacker with a session would do.
    """
    device = totp_device(user, confirmed=True)
    if device is None:
        raise InvalidState("Two-factor authentication is not enabled.")

    if not verify(user=user, code=code):
        raise NotAllowed("That code is not correct.")

    TOTPDevice.objects.filter(user=user).delete()
    StaticDevice.objects.filter(user=user, name=RECOVERY_DEVICE_NAME).delete()
    audit.record(verb="account.mfa_disabled", actor=user, target=user)


def verify(*, user: User, code: str) -> bool:
    """Verify a TOTP code or a recovery code.

    Recovery codes are consumed by the plugin on use, so each works once.
    """
    return verified_device(user=user, code=code) is not None


def verified_device(*, user: User, code: str) -> TOTPDevice | StaticDevice | None:
    """The device a correct code belongs to, or None.

    The sign-in challenge needs the device itself: `django_otp.login` records it
    on the session, which is what makes `request.user.is_verified()` true.
    """
    code = (code or "").strip().replace(" ", "")
    if not code:
        return None
    device = totp_device(user, confirmed=True)
    if device is not None and device.verify_token(code):
        return device

    recovery = StaticDevice.objects.filter(user=user, name=RECOVERY_DEVICE_NAME).first()
    if recovery is not None and recovery.verify_token(code):
        audit.record(verb="account.mfa_recovery_code_used", actor=user, target=user)
        return recovery

    return None


@transaction.atomic
def regenerate_recovery_codes(*, user: User) -> list[str]:
    if not is_enabled(user):
        raise InvalidState("Two-factor authentication is not enabled.")
    codes = _issue_recovery_codes(user)
    audit.record(verb="account.mfa_recovery_codes_regenerated", actor=user, target=user)
    return codes


def remaining_recovery_codes(*, user: User) -> int:
    device = StaticDevice.objects.filter(user=user, name=RECOVERY_DEVICE_NAME).first()
    return device.token_set.count() if device else 0


def _issue_recovery_codes(user: User) -> list[str]:
    device, _ = StaticDevice.objects.get_or_create(user=user, name=RECOVERY_DEVICE_NAME)
    device.token_set.all().delete()

    codes = [secrets.token_hex(5) for _ in range(RECOVERY_CODE_COUNT)]
    StaticToken.objects.bulk_create([StaticToken(device=device, token=code) for code in codes])
    return codes


def provisioning_uri(device: TOTPDevice) -> str:
    """The otpauth:// URI for an authenticator app.

    No QR image is rendered: that needs either a Python imaging dependency or a
    JavaScript one, and every authenticator accepts manual entry. The setup
    screen shows this URI and the grouped secret.
    """
    return device.config_url
