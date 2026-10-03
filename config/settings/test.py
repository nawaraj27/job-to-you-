"""
Test settings.

Still PostgreSQL (ADR-A2): the constraints, partial indexes and SKIP LOCKED
behaviour under test must be the ones that run in production. What changes here
is only what makes tests fast and hermetic.
"""

import os
import tempfile
from pathlib import Path

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-only-key-not-secret"  # noqa: S105 - not a credential
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Two `manage.py test` runs against one test database destroy each other (one
# drops the database the other is using). A second run can name its own.
if os.environ.get("CAREERAI_TEST_DB_NAME"):
    DATABASES["default"]["TEST"] = {"NAME": os.environ["CAREERAI_TEST_DB_NAME"]}  # noqa: F405

# Local-memory cache so tests never share state through a real Redis, and so a
# missing Redis does not fail the suite. Sessions follow the cache.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "careerai-test",
    },
    "sessions": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "careerai-test-sessions",
    },
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

_TEST_MEDIA = Path(tempfile.mkdtemp(prefix="careerai-test-"))
MEDIA_ROOT = _TEST_MEDIA / "public"
PRIVATE_STORAGE_ROOT = _TEST_MEDIA / "private"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": str(MEDIA_ROOT), "base_url": MEDIA_URL},  # noqa: F405
    },
    "private": {
        "BACKEND": "apps.files.storage.PrivateFileSystemStorage",
        "OPTIONS": {"location": str(PRIVATE_STORAGE_ROOT)},
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Tests pick their scanner explicitly with override_settings; the default here
# is the permissive one so unrelated tests are not blocked on a daemon.
FILES_SCANNER = "apps.files.scanning.AllowAllScanner"

# DEBUG is off here so tests exercise production behaviour, which makes the
# deploy check correctly object to the permissive scanner above. Silenced only
# in this module — the tests that assert the check fires call it directly, so
# the guard itself stays covered.
SILENCED_SYSTEM_CHECKS = ["files.E002"]

LOGGING["root"]["level"] = "CRITICAL"  # noqa: F405

# Fixed so the suite is hermetic and does not depend on the environment.
# Nothing real is ever encrypted under it.
FIELD_ENCRYPTION_KEYS = os.environ.get(
    "FIELD_ENCRYPTION_KEYS", "kQ8sT2vX9pL4nR7yB1cM6wZ0aD3fG5hJ8kN2qS4tV6Y="
).split(",")

# Tests never load a speech model.
SPEECH_TO_TEXT_PROVIDER = "apps.interviews.speech.fake.FakeTranscriber"
SPEECH_TO_TEXT_OPTIONS: dict = {}

# No test may reach a real payment gateway. A test answers as the gateway would
# through `apps.billing.gateways.base.set_transport`; anything unanswered fails
# as a connection error (ADR-A58).
PAYMENTS_BLOCK_NETWORK = True
PAYMENTS_ENVIRONMENT = "test"
# Off unless a test turns one on: a developer's `.env` (which may enable the
# eSewa sandbox) must not change what the suite sees.
PAYMENT_GATEWAYS = {
    "esewa": {"enabled": False},
    "khalti": {"enabled": False},
}
