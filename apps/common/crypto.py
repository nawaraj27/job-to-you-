"""
Field-level encryption for the few columns that need it (DEVELOPER.md §6.11).

A candidate's phone number is the one contact detail this platform holds that
is directly actionable by anyone who reads it, and ADR-A34 exists precisely
because employers must not see it before a stage unlocks it. Application-level
encryption is what makes that promise survive a database backup, a read-replica
snapshot, or a support engineer with `psql`.

`cryptography`'s Fernet is used rather than an in-tree AES wrapper: this project
already decided once (ADR-A42) that cryptographic code benefits more from
community review than from one fewer dependency, and nothing about that argument
changes here.

**Rotation is built in.** `FIELD_ENCRYPTION_KEYS` is a list. The first key
encrypts; every key decrypts. Rotating means prepending a new key, re-saving the
affected rows, then dropping the old key — with no window where existing data is
unreadable.

**What this is not.** Fernet output is randomised, so two encryptions of the
same number differ. Encrypted columns therefore support no equality lookup, no
index and no uniqueness. That is a deliberate trade: a searchable encrypted
phone number is a decrypted phone number with extra steps. Search on a separate
blind index if the need ever arises.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


@lru_cache(maxsize=1)
def _cipher() -> MultiFernet:
    keys = getattr(settings, "FIELD_ENCRYPTION_KEYS", None) or []
    if not keys:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEYS is empty, so encrypted fields cannot be read or written. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            f"FIELD_ENCRYPTION_KEYS contains a value that is not a Fernet key: {exc}"
        ) from exc


def reset_cipher_cache() -> None:
    """Forget the cached cipher. For tests that override the key setting."""
    _cipher.cache_clear()


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    return _cipher().decrypt(token.encode("ascii")).decode("utf-8")


class EncryptedCharField(models.TextField):
    """A short string stored encrypted.

    `TextField` underneath because ciphertext is longer than plaintext and by a
    ratio nobody should have to compute; `max_length` still applies to the
    *plaintext* through validation, so forms and admin behave normally.

    Empty stays empty. Encrypting `""` would produce a non-empty ciphertext, and
    every `if not profile.phone` in the codebase would quietly become false.
    """

    description = "Encrypted short text"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Applied to the plaintext by the form layer; the column itself is TEXT.
        self.plaintext_max_length = kwargs.pop("max_length", 40)
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> Any:
        name, path, args, kwargs = super().deconstruct()
        kwargs["max_length"] = self.plaintext_max_length
        return name, path, args, kwargs

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> Any:
        if value is None or value == "":
            return value
        try:
            return decrypt(value)
        except InvalidToken:
            # Loud rather than silent: a value no current key can read means a
            # key was dropped too early, and returning the ciphertext would put
            # it on screen as if it were the number.
            raise ValueError(
                "An encrypted field could not be decrypted with any configured key. "
                "A key was probably rotated out before the rows were re-saved."
            ) from None

    def get_prep_value(self, value: Any) -> Any:
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt(str(value))

    def formfield(self, **kwargs: Any) -> Any:
        kwargs.setdefault("max_length", self.plaintext_max_length)
        return super().formfield(**kwargs)
