"""
Hashing helpers.

Two jobs, deliberately separate:

  * content hashing  — SHA-256 of bytes on disk, used for file deduplication,
    render caching and the submitted-CV guarantee (ADR-A8). Must be a real
    cryptographic digest because it is evidence.

  * stable hashing   — a canonical digest of a Python structure, used as a
    cache key (AI input hashes, idempotency request fingerprints). Must be
    stable across processes and across runs, which rules out hash().
"""

from __future__ import annotations

import hashlib
import json
from typing import IO, Any

CHUNK_SIZE = 64 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: IO[bytes]) -> str:
    """Digest a file-like object without loading it into memory.

    Reads from the current position and restores it, so callers can hash an
    uploaded file and still save it afterwards.
    """
    position = stream.tell() if stream.seekable() else None
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
        digest.update(chunk)
    if position is not None:
        stream.seek(position)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    """Deterministic digest of a JSON-serialisable structure.

    Keys are sorted and separators fixed so that two structurally equal inputs
    always produce the same digest, in this process and in the next release.
    """
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
