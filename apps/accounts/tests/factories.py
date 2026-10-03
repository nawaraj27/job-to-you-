"""
Account factories.

Plain functions rather than a factory library: the project has one user model
with four fields, and a dependency would be larger than the code it replaces.
Factories create rows directly, which is not a service bypass — they do not
claim to be domain operations (DEVELOPER.md §8.2).
"""

from __future__ import annotations

import itertools

from apps.accounts.models import User

_counter = itertools.count(1)


def create_user(**overrides) -> User:
    index = next(_counter)
    fields = {
        "email": f"user{index}@example.com",
        "full_name": f"Test User {index}",
        "password": "test-password-not-a-secret",
    }
    fields.update(overrides)
    password = fields.pop("password")
    user = User(**fields)
    user.set_password(password)
    user.save()
    return user


def create_staff(**overrides) -> User:
    overrides.setdefault("is_staff", True)
    overrides.setdefault("signup_intent", User.SignupIntent.STAFF)
    return create_user(**overrides)
