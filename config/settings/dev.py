"""Local development. Postgres and Redis come from docker-compose."""

import os

from .base import *  # noqa: F403
from .base import env_bool, env_list

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = ["*"]

# A tunnel (ngrok) serves the local site over HTTPS on another origin, and
# Django refuses a form POST whose Origin it does not trust. Set the tunnel's
# address in `.env` — `DJANGO_CSRF_TRUSTED_ORIGINS=https://<name>.ngrok-free.dev`
# — rather than here: the address changes whenever the tunnel is restarted.
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# The database stays PostgreSQL (ADR-A2 — pgvector is required; SQLite cannot
# run this project). It comes from docker-compose: `docker compose up -d`.

_REDIS = os.environ.get("REDIS_URL", "redis://127.0.0.1:6380")
CACHES["default"]["LOCATION"] = os.environ.get("REDIS_CACHE_URL", f"{_REDIS}/1")  # noqa: F405
CACHES["sessions"]["LOCATION"] = os.environ.get("REDIS_SESSION_URL", f"{_REDIS}/2")  # noqa: F405
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", f"{_REDIS}/0")

# In development, never let the browser hold a static file. WhiteNoise defaults
# to 60 seconds; in production filenames are hashed so a changed file is a
# changed URL, but locally a stale asset reads as "my change did nothing".
WHITENOISE_MAX_AGE = 0

STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
}

# No ClamAV daemon locally. AllowAllScanner still walks the full scan state
# machine (pending -> clean) so the code path under test is the real one; a
# system check refuses to let it run with DEBUG off (apps/files/checks.py).
FILES_SCANNER = os.environ.get("FILES_SCANNER", "apps.files.scanning.AllowAllScanner")

INTERNAL_IPS = ["127.0.0.1"]

# Local only. The dev database holds invented phone numbers; a fixed key
# keeps them readable across restarts, which is what makes the field debuggable.
FIELD_ENCRYPTION_KEYS = os.environ.get(
    "FIELD_ENCRYPTION_KEYS", "5o0oq0Wc2mS3wBb0HqfM7g9dQ1x9nQ2wS8sVLpYK4nE="
).split(",")
