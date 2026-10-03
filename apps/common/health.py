"""
The dependency probes, in one place (SRS §17.4).

§17.4 asks for `/readyz` to cover the database, Redis and storage, and for the
operations board to be the screen somebody opens during an incident. Both
answer the same question, so both ask the same code: two definitions of "is
private storage reachable" is one more than the number of answers that question
has, and the one that mattered would be whichever surface was being looked at.

**A probe reports; it never raises.** A readiness endpoint that 500s is
indistinguishable from a dead process, and an incident board that 500s is worse
than no board.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from django.core.cache import cache
from django.core.files.storage import storages
from django.db import connection

logger = logging.getLogger(__name__)

OK = "ok"


@dataclass(frozen=True)
class Probe:
    """One dependency, its machine name and the words a person reads."""

    key: str
    label: str
    run: Callable[[], None]


def database() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def cache_backend() -> None:
    cache.set("readyz", "1", 5)
    if cache.get("readyz") != "1":
        raise RuntimeError("cache round-trip failed")


def private_storage() -> None:
    # exists() on a name that will never be created proves the backend is
    # configured and reachable without writing anything.
    storages["private"].exists(".readyz-probe")


#: The order they are reported in, weakest dependency last. Named here rather
#: than assembled per caller, so a probe added for the endpoint appears on the
#: board as well.
PROBES: tuple[Probe, ...] = (
    Probe("database", "Database", database),
    Probe("cache", "Cache and sessions", cache_backend),
    Probe("private_storage", "Private storage", private_storage),
)


def check(probe: Probe) -> str:
    """Run one probe and turn any failure into a reportable string.

    The name comes off the `Probe` rather than the callable's `__name__`:
    reflecting on the object it was handed makes readiness depend on the shape
    of that object, which is the kind of incidental coupling that breaks at the
    worst possible moment.
    """
    try:
        probe.run()
    except Exception as exc:  # noqa: BLE001 - a readiness probe reports, never raises
        logger.warning("readiness check failed", extra={"probe": probe.key})
        return f"error: {type(exc).__name__}"
    return OK


def check_all() -> list[tuple[Probe, str]]:
    return [(probe, check(probe)) for probe in PROBES]
