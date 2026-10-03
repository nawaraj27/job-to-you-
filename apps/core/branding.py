"""
The product's name, for code that is not a template (docs/BRANDING.md).

Templates read `{{ site.name }}` from the `site` context processor; emails,
messages and generated files read `site_name()`. Both come from
`settings.SITE_NAME`, so renaming the site is one environment variable.
"""

from __future__ import annotations

from django.conf import settings


def site_name() -> str:
    return settings.SITE_NAME


def support_email() -> str:
    return settings.SITE_SUPPORT_EMAIL
