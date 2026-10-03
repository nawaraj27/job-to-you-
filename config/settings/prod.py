"""Production. Every secret comes from the environment; nothing falls back."""

import os

from .base import *  # noqa: F403
from .base import env_bool, env_list, env_required

DEBUG = False

SECRET_KEY = env_required("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS is required in production.")

DATABASES["default"]["PASSWORD"] = env_required("POSTGRES_PASSWORD")  # noqa: F405

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Scanning is mandatory in production; apps/files/checks.py fails the deploy if
# this is left on a permissive backend.
FILES_SCANNER = os.environ.get("FILES_SCANNER", "apps.files.scanning.ClamAVScanner")
