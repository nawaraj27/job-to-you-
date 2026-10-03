"""
UUIDv7 (RFC 9562 §5.7).

Every externally visible identifier is a UUIDv7 (SRS §5), so sequential
primary keys never leak row counts. Version 7 rather than 4 because it is
time-ordered: values generated close together sort close together, which keeps
B-tree inserts local instead of scattering them across the index.

Python's stdlib gained uuid.uuid7() in 3.14; this project targets 3.12, so the
generator is implemented here and delegates to the stdlib when available.

Layout:
    48 bits  unix_ts_ms
     4 bits  version (7)
    12 bits  rand_a       — used here as a sub-millisecond counter
     2 bits  variant (0b10)
    62 bits  rand_b       — random

The counter in rand_a guarantees monotonicity within a millisecond, so two
identifiers generated in the same millisecond still order by creation.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_stdlib_uuid7 = getattr(uuid, "uuid7", None)

_lock = threading.Lock()
_last_timestamp_ms = -1
_counter = 0

_MAX_COUNTER = 0xFFF  # 12 bits of rand_a


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUIDv7. Monotonic within a process."""
    if _stdlib_uuid7 is not None:  # pragma: no cover - depends on interpreter
        return _stdlib_uuid7()

    global _last_timestamp_ms, _counter

    with _lock:
        timestamp_ms = time.time_ns() // 1_000_000

        if timestamp_ms == _last_timestamp_ms:
            _counter += 1
            if _counter > _MAX_COUNTER:
                # More than 4096 ids in one millisecond: wait for the clock.
                while timestamp_ms <= _last_timestamp_ms:
                    timestamp_ms = time.time_ns() // 1_000_000
                _counter = 0
        elif timestamp_ms < _last_timestamp_ms:
            # Clock moved backwards (NTP step). Keep issuing monotonic values
            # from the last timestamp rather than reusing an earlier window.
            timestamp_ms = _last_timestamp_ms
            _counter += 1
            if _counter > _MAX_COUNTER:
                timestamp_ms += 1
                _counter = 0
        else:
            _counter = 0

        _last_timestamp_ms = timestamp_ms
        counter = _counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = (
        (timestamp_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | (counter & _MAX_COUNTER) << 64
        | 0b10 << 62
        | rand_b
    )
    return uuid.UUID(int=value)


__all__ = ["uuid7"]
