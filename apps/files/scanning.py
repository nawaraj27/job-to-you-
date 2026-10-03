"""
Virus scanning (SRS §14.2, DEVELOPER.md §6.4).

Scanning happens before any processing. Nothing parses, renders or embeds a
file that has not come back clean.

Two backends ship:

  ClamAVScanner    production. Speaks clamd's INSTREAM protocol over TCP, so
                   the daemon can live in its own container and no file is ever
                   handed to it by path.
  AllowAllScanner  development and tests. Returns clean without inspecting the
                   file, and is refused at startup when DEBUG is off
                   (apps/files/checks.py). It exists so the scan state machine
                   — pending, then clean or quarantined — is the same code path
                   locally as in production, rather than being skipped.

`get_scanner()` resolves the configured backend once per process.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import IO, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    signature: str = ""
    error: str = ""

    @property
    def errored(self) -> bool:
        return bool(self.error)


class Scanner(Protocol):
    def scan(self, stream: IO[bytes]) -> ScanResult: ...


class AllowAllScanner:
    """Passes everything. Development and tests only."""

    permissive = True

    def scan(self, stream: IO[bytes]) -> ScanResult:
        return ScanResult(clean=True, signature="")


class InfectedScanner:
    """Fails everything. Used by tests to exercise the quarantine path."""

    permissive = True

    def scan(self, stream: IO[bytes]) -> ScanResult:
        return ScanResult(clean=False, signature="Test-Signature")


class ClamAVScanner:
    """clamd INSTREAM client.

    The wire protocol: send `zINSTREAM\\0`, then a sequence of chunks, each
    prefixed with its length as a four-byte big-endian integer, terminated by a
    zero-length chunk. The daemon replies with a single line ending in `OK`,
    `FOUND` or `ERROR`.

    A scan that cannot complete returns an errored result rather than raising.
    The caller records `error` status and quarantines: an unscannable file is
    treated as unsafe, never as clean.
    """

    permissive = False
    MAX_RESPONSE = 4096

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: int | None = None,
    ) -> None:
        self.host = host or settings.CLAMAV_HOST
        self.port = port or settings.CLAMAV_PORT
        self.timeout = timeout or settings.CLAMAV_TIMEOUT

    def scan(self, stream: IO[bytes]) -> ScanResult:
        position = stream.tell() if stream.seekable() else None
        try:
            response = self._instream(stream)
        except (TimeoutError, OSError) as exc:
            logger.warning("clamav unreachable", extra={"error": str(exc)})
            return ScanResult(clean=False, error=f"scanner unavailable: {exc}")
        finally:
            if position is not None and stream.seekable():
                stream.seek(position)

        if response.endswith("OK"):
            return ScanResult(clean=True)
        if response.endswith("FOUND"):
            signature = response.removeprefix("stream:").removesuffix("FOUND").strip()
            return ScanResult(clean=False, signature=signature)
        return ScanResult(clean=False, error=response or "empty response")

    def _instream(self, stream: IO[bytes]) -> str:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.sendall(b"zINSTREAM\0")
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
            sock.sendall((0).to_bytes(4, "big"))

            data = b""
            while b"\0" not in data and len(data) < self.MAX_RESPONSE:
                received = sock.recv(self.MAX_RESPONSE)
                if not received:
                    break
                data += received

        return data.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


@lru_cache(maxsize=1)
def get_scanner() -> Scanner:
    return import_string(settings.FILES_SCANNER)()


def reset_scanner_cache() -> None:
    """Used by tests that swap FILES_SCANNER with override_settings."""
    get_scanner.cache_clear()
