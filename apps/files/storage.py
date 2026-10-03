"""
Storage backends.

`PrivateFileSystemStorage` exists because of a sharp edge in Django's default:
`FileSystemStorage.base_url` falls back to `settings.MEDIA_URL` when it is not
given explicitly. A private backend configured with only a `location` therefore
still produces `/media/<key>` from `.url()` — a real, guessable URL for a
resume, bypassing the policy check, the single-use token and the access log.

That is precisely the failure SRS §14.2 is written to prevent, and it happens
silently, so it is closed here at the type level: this backend has no URL, and
asking for one raises.
"""

from __future__ import annotations

from django.core.files.storage import FileSystemStorage


class PrivateFileSystemStorage(FileSystemStorage):
    """Local storage for files that must never be addressable."""

    def url(self, name: str | None) -> str:
        raise ValueError(
            "Private attachments have no direct URL. Serve them through "
            "apps.files.views.FileAccessView, which checks the policy and logs the read."
        )
