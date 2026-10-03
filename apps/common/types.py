"""
Shared type aliases.

`Actor` is the type of "whoever is doing this". It includes AnonymousUser
because policies must answer questions about signed-out visitors, and None
because services are also called by scheduled jobs and management commands
where there is no user at all.

Services that persist an actor take `User | None` instead: a foreign key cannot
hold an AnonymousUser, so the normalisation happens at the edge — in the view —
rather than being rediscovered inside every service.

Imported under TYPE_CHECKING so that `common` keeps no runtime dependency on a
domain app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser

    from apps.accounts.models import User

    # Declared as an alias explicitly: inferred, it degrades to a plain variable
    # whenever an import cycle leaves `User` unresolved at this point.
    type Actor = User | AnonymousUser | None
else:  # pragma: no cover - runtime placeholder, never introspected
    Actor = object
