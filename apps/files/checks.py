"""
Deployment safety checks.

Both checks guard misconfigurations that survive review because nothing
visibly breaks: a permissive virus scanner, and a private storage backend that
quietly hands out URLs. `manage.py check` runs in CI and at container start, so
these fail the deploy rather than the first malicious upload or the first
indexed resume.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register
from django.core.files.storage import storages
from django.utils.module_loading import import_string


@register("files")
def check_scanner_is_not_permissive(app_configs, **kwargs):
    if settings.DEBUG:
        return []

    try:
        scanner_class = import_string(settings.FILES_SCANNER)
    except ImportError:
        return [
            Error(
                f"FILES_SCANNER points at {settings.FILES_SCANNER!r}, which cannot be imported.",
                id="files.E001",
            )
        ]

    if getattr(scanner_class, "permissive", False):
        return [
            Error(
                "A permissive virus scanner is configured with DEBUG off. "
                f"FILES_SCANNER is {settings.FILES_SCANNER!r}, which accepts every file "
                "without inspecting it.",
                hint="Set FILES_SCANNER to apps.files.scanning.ClamAVScanner and run a clamd daemon.",
                id="files.E002",
            )
        ]
    return []


@register("files")
def check_private_storage_has_no_public_url(app_configs, **kwargs):
    """Private files must have no address of their own.

    The check probes the configured backend rather than reading its options,
    because the dangerous case is invisible in configuration: Django's
    FileSystemStorage falls back to MEDIA_URL when no base_url is given, so a
    backend that looks correctly configured still returns `/media/<key>` — a
    guessable URL that bypasses the policy check, the single-use token and the
    access log (SRS §14.2).
    """
    try:
        storage = storages["private"]
    except Exception as exc:  # noqa: BLE001 - a broken alias is itself the finding
        return [
            Error(
                f"The 'private' storage backend could not be loaded: {exc}",
                id="files.E004",
            )
        ]

    try:
        url = storage.url("probe/not-a-real-object")
    except (ValueError, NotImplementedError):
        return []  # No URL. Correct.
    except Exception:  # noqa: BLE001
        return []  # Backend-specific refusal is also acceptable.

    if url and not url.startswith(("http://", "https://")):
        return [
            Error(
                "The 'private' storage backend returns a local URL "
                f"({url!r}), so private files are reachable without a policy check.",
                hint=(
                    "Use apps.files.storage.PrivateFileSystemStorage locally, or an object "
                    "store that issues expiring signed URLs."
                ),
                id="files.E003",
            )
        ]

    location = str(settings.STORAGES.get("private", {}).get("OPTIONS", {}).get("location", ""))
    media_root = str(settings.MEDIA_ROOT)
    if location and media_root and location.startswith(media_root):
        return [
            Warning(
                "Private storage lives inside MEDIA_ROOT, so a static file server may expose it.",
                hint="Point PRIVATE_STORAGE_ROOT outside MEDIA_ROOT.",
                id="files.W001",
            )
        ]
    return []
