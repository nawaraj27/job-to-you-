"""
Base settings — shared by every environment.

Environment-specific modules (dev, prod, test) import from here and override.
Nothing in this file reads a value that only one environment can supply; if a
setting differs by environment, it is defined here with the safe default and
overridden there.

Selected by DJANGO_SETTINGS_MODULE, which defaults to config.settings.dev in
manage.py and config.settings.prod in the container image.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# --- .env ----------------------------------------------------------------
#
# `.env.example` has always said "copy to .env", and nothing read it: every
# value in that file was inert unless it also happened to be exported in the
# shell. A developer who followed the README got an application configured
# entirely by defaults, which is how a machine with only one local model
# pulled ends up calling a model it does not have.
#
# Real environment variables win — a container, a CI job and a production
# process all supply configuration that way, and a stray `.env` on the host
# must not be able to override it.


def _load_dotenv(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


# --- environment helpers -------------------------------------------------


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def env_required(name: str) -> str:
    """For values that must never fall back to a development default."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required in this environment and is not set.")
    return value


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")


# --- applications --------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Third party
    "widget_tweaks",
    # MFA (ADR-A42). otp_totp provides the authenticator device; otp_static
    # provides recovery codes.
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    # Platform / shared (SRS §6.2)
    "apps.common",
    "apps.audit",
    "apps.files",
    "apps.core",
    # Domain
    "apps.accounts",
    "apps.companies",
    "apps.taxonomy",
    "apps.candidates",
    "apps.resumes",
    "apps.jobs",
    "apps.applications",
    "apps.messaging",
    "apps.interviews",
    "apps.ai",
    "apps.matching",
    "apps.analytics",
    "apps.billing",
    "apps.notifications",
    # Portals
    "portals.employer",
    "portals.candidate",
    "portals.staff",
]

MIDDLEWARE = [
    # First, so that every log line and every exception raised by the layers
    # below carries a correlation id — including authentication failures,
    # which previously logged with no id at all because this sat last.
    "apps.common.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must follow authentication: it decorates request.user with is_verified().
    "django_otp.middleware.OTPMiddleware",
    # After OTP (the staff member's own verification is what is borrowed) and
    # before tenancy, which must resolve memberships for the person being
    # viewed, not for the staff member (FR-1.10).
    "apps.accounts.impersonation.ImpersonationMiddleware",
    # Must follow authentication: tenancy is resolved from the URL against an
    # active membership (ADR-A18).
    "apps.companies.middleware.CompanyContextMiddleware",
    # Maps DomainError to a response in exactly one place (DEVELOPER.md §3.7).
    "apps.common.errors_middleware.DomainErrorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # After tenancy (its process_view needs the URL's membership: an employer
    # page is protected only for owners and admins of that company) and after
    # messages, which it uses to say why a page did not open (FR-1.5).
    "apps.accounts.middleware.MfaRequirementMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Last, so the nonce is attached to the finished response.
    "apps.common.csp.ContentSecurityPolicyMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site",
                "apps.core.context_processors.navigation",
                "apps.core.context_processors.preferences",
                "apps.accounts.impersonation.banner",
                "apps.common.csp.csp_nonce",
            ],
            "builtins": [
                "apps.core.templatetags.icons",
                "apps.core.templatetags.ui",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# --- database (ADR-A2: PostgreSQL 16 + pgvector in every environment) ----

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "careerai"),
        "USER": os.environ.get("POSTGRES_USER", "careerai"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "careerai"),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
        "OPTIONS": {
            # Fail fast rather than hanging a web worker on a wedged database.
            "connect_timeout": 5,
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"


# --- cache, sessions, queue ----------------------------------------------

# Redis is separated by role (ADR-A35). One keyspace previously served the
# cache, the sessions and the broker, so `cache.clear()` logged everyone out
# and a fan-out backlog could evict session data.
#
# What the split does and does not buy, stated precisely because the
# distinction matters: separate logical databases give namespace isolation —
# `cache.clear()` reaches only the cache, and each role can be monitored and
# flushed independently. They do NOT give separate eviction policies;
# `maxmemory-policy` is instance-wide.
#
# Eviction is no longer a correctness problem, because the two things that
# could not survive it were moved out of Redis entirely: single-use token
# state now lives in PostgreSQL (ADR-A38) and sessions use cached_db, so the
# cache is a read cache over a durable table. The instance runs volatile-lru,
# which evicts only keys carrying a TTL — cache and session entries, both
# recoverable from the database — and never the broker keys, which have none
# and whose loss would mean losing queued work.
REDIS_HOST_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
REDIS_BROKER_URL = os.environ.get("REDIS_BROKER_URL", f"{REDIS_HOST_URL}/0")
REDIS_CACHE_URL = os.environ.get("REDIS_CACHE_URL", f"{REDIS_HOST_URL}/1")
REDIS_SESSION_URL = os.environ.get("REDIS_SESSION_URL", f"{REDIS_HOST_URL}/2")

#: Seconds before a Redis call gives up. Without these redis-py waits on the
#: operating system and retries, and a stopped Redis turned every page load into
#: a thirty-eight-second hang before the error (OPS-01). A cache that cannot
#: answer in a second is not a cache.
_REDIS_TIMEOUTS = {"socket_connect_timeout": 1, "socket_timeout": 1}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "KEY_PREFIX": "careerai",
        "OPTIONS": _REDIS_TIMEOUTS,
    },
    "sessions": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_SESSION_URL,
        "KEY_PREFIX": "session",
        "OPTIONS": _REDIS_TIMEOUTS,
    },
}

# cached_db rather than cache: this deployment runs Redis without persistence,
# so a cache-only session store logs every user out on each deploy or restart.
# Reads still come from Redis; the database is the durable copy — and
# `apps.accounts.sessions` makes that true when Redis is down, which Django's
# own backend does not.
SESSION_ENGINE = "apps.accounts.sessions"
SESSION_CACHE_ALIAS = "sessions"
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_BROKER_URL)
CELERY_RESULT_BACKEND = None  # Fire-and-forget; state lives in the database.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_TIME_LIMIT = env_int("CELERY_TASK_TIME_LIMIT", 600)
CELERY_TASK_SOFT_TIME_LIMIT = env_int("CELERY_TASK_SOFT_TIME_LIMIT", 540)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True


# --- authentication ------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2 first (DEVELOPER.md §6.1). Django ships the hasher; argon2-cffi is an
# optional extra and is installed in requirements.txt.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

# --- email ---------------------------------------------------------------
#
# The provider, templates and suppression list are P10. P1 needs verification
# and invitation mail, so the console backend is the development default and
# SMTP is configured from the environment in production.
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", f"{os.environ.get('SITE_NAME', 'Kaarya')} <hello@careerai.app>"
)

PASSWORD_RESET_TIMEOUT = env_int("PASSWORD_RESET_TIMEOUT", 60 * 60 * 24)

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "core:home"


# --- i18n / time ---------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kathmandu"  # Display default only; storage is UTC (SRS §1.4)
USE_I18N = True
USE_TZ = True


# --- static and files ----------------------------------------------------

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Public media only. Private attachments never live here and are never served
# by the static server (SRS §14.2) — they are reachable exclusively through
# apps.files.views.FileAccessView.
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "var" / "public"

PRIVATE_STORAGE_ROOT = Path(os.environ.get("PRIVATE_STORAGE_ROOT", BASE_DIR / "var" / "private"))

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": str(MEDIA_ROOT), "base_url": MEDIA_URL},
    },
    # A backend with no URL at all. Django's FileSystemStorage would fall
    # back to MEDIA_URL here, handing every private file a guessable address
    # (see apps/files/storage.py).
    "private": {
        "BACKEND": "apps.files.storage.PrivateFileSystemStorage",
        "OPTIONS": {"location": str(PRIVATE_STORAGE_ROOT)},
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE

# --- spoken practice interviews ------------------------------------------
# Empty means: the local faster-whisper engine when it is installed, and no
# speech recognition otherwise (candidates then type). Audio is transcribed and
# deleted within the request; nothing is stored (apps/interviews/speech).
SPEECH_TO_TEXT_PROVIDER = os.environ.get("SPEECH_TO_TEXT_PROVIDER", "")
SPEECH_TO_TEXT_OPTIONS = {"model": os.environ.get("SPEECH_TO_TEXT_MODEL", "base.en")}

# --- live interview video rooms -------------------------------------------
# The fake provider is the default so a laptop can book interviews end to end.
# Production must select Jitsi and configure JaaS; `manage.py check --deploy`
# refuses the fake (interviews.E001) and an incomplete JaaS setup (E002).
VIDEO_ROOM_PROVIDER = os.environ.get(
    "VIDEO_ROOM_PROVIDER", "apps.interviews.providers.fake.FakeRoomProvider"
)
VIDEO_ROOM_OPTIONS = {
    "app_id": os.environ.get("JAAS_APP_ID", ""),
    "api_key_id": os.environ.get("JAAS_API_KEY_ID", ""),
    "private_key": os.environ.get("JAAS_PRIVATE_KEY", ""),
    "private_key_path": os.environ.get("JAAS_PRIVATE_KEY_PATH", ""),
    "base_url": os.environ.get("JAAS_BASE_URL", "https://8x8.vc"),
}


# --- files subsystem (apps.files) ----------------------------------------

FILES_MAX_UPLOAD_BYTES = env_int("FILES_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
FILES_SIGNED_URL_MAX_AGE = env_int("FILES_SIGNED_URL_MAX_AGE", 300)  # 5 minutes
FILES_SCANNER = os.environ.get("FILES_SCANNER", "apps.files.scanning.ClamAVScanner")
CLAMAV_HOST = os.environ.get("CLAMAV_HOST", "127.0.0.1")
CLAMAV_PORT = env_int("CLAMAV_PORT", 3310)
CLAMAV_TIMEOUT = env_int("CLAMAV_TIMEOUT", 30)


# --- MFA in the admin -----------------------------------------------------
#
# django-otp's admin shows a TOTP device's secret key, its QR code and static
# backup tokens by default. Anyone who can open that page could clone a staff
# member's second factor. Hidden here; `apps.accounts.admin` also makes the
# devices view-only.
OTP_ADMIN_HIDE_SENSITIVE_DATA = True


# --- branding -------------------------------------------------------------
#
# The product's name, in one place. Every page, email subject and generated
# document reads it from here (`apps.core.branding`), so a rename is one
# environment variable (docs/BRANDING.md). The tagline is the one on the logo.
SITE_NAME = os.environ.get("SITE_NAME", "Kaarya")
SITE_TAGLINE = os.environ.get("SITE_TAGLINE", "CV, Jobs → Opportunities")
SITE_SUPPORT_EMAIL = os.environ.get("SITE_SUPPORT_EMAIL", "hello@careerai.app")


# --- CV drafts (docs/CV-Lifecycle.md) -------------------------------------
# An unsaved draft nobody edits for this many days is removed by the nightly
# `purge_abandoned_drafts` task. Saved and archived CVs are never touched.
RESUME_DRAFT_RETENTION_DAYS = env_int("RESUME_DRAFT_RETENTION_DAYS", 14)


# --- payment gateways (ADR-A55, ADR-A58) ---------------------------------
#
# Test mode only. `PAYMENTS_ENVIRONMENT` must be "test": the gateway URLs below
# are the providers' published sandbox hosts, and `billing.E00x` system checks
# refuse anything else (a live merchant account is a business decision this
# build has not been given). A gateway is offered to customers only when it is
# enabled *and* its credentials are present; an enabled gateway with a missing
# key is a check error, not a button that fails after the customer presses it.
#
# No credential has a default here. eSewa publishes its UAT secret key and test
# product code (developer.esewa.com.np, "Epay"); Khalti issues a sandbox
# `live_secret_key` per merchant at test-admin.khalti.com. Both go in `.env`.
PAYMENTS_ENVIRONMENT = os.environ.get("PAYMENTS_ENVIRONMENT", "test")
PAYMENTS_HTTP_TIMEOUT = env_int("PAYMENTS_HTTP_TIMEOUT", 15)
PAYMENT_GATEWAYS = {
    "esewa": {
        "enabled": env_bool("ESEWA_ENABLED", False),
        "product_code": os.environ.get("ESEWA_PRODUCT_CODE", ""),
        "secret_key": os.environ.get("ESEWA_SECRET_KEY", ""),
        "form_url": os.environ.get(
            "ESEWA_FORM_URL", "https://rc-epay.esewa.com.np/api/epay/main/v2/form"
        ),
        "status_url": os.environ.get(
            "ESEWA_STATUS_URL", "https://rc.esewa.com.np/api/epay/transaction/status/"
        ),
    },
    "khalti": {
        "enabled": env_bool("KHALTI_ENABLED", False),
        "secret_key": os.environ.get("KHALTI_SECRET_KEY", ""),
        "base_url": os.environ.get("KHALTI_BASE_URL", "https://dev.khalti.com/api/v2/"),
    },
}


# --- field-level encryption (DEVELOPER.md §6.11) --------------------------
#
# A list, because rotation needs two keys live at once: the first encrypts,
# every one of them decrypts. Generate a key with:
#     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#
# No default here. A default key is a published key, and encrypted phone
# numbers under a key that ships in the repository are plaintext with an
# inconvenience. dev/test settings supply their own throwaway value; production
# fails a system check without one (apps/common/checks.py).
FIELD_ENCRYPTION_KEYS = env_list("FIELD_ENCRYPTION_KEYS")


# --- rendering (SRS §5.5.2, P6) ------------------------------------------
#
# Where an operator drops licensed TTFs for embedding. Empty by default: the
# renderer falls back to the PDF base-14 families, which every reader has and
# nobody has to license, and records on each render which it actually used.
# Shipping font binaries in the repository is a licensing decision, so this
# project does not make it silently.
RENDER_FONT_DIR = os.environ.get("RENDER_FONT_DIR", "")


# --- outbox (ADR-A14) ----------------------------------------------------

OUTBOX_BATCH_SIZE = env_int("OUTBOX_BATCH_SIZE", 100)
OUTBOX_MAX_ATTEMPTS = env_int("OUTBOX_MAX_ATTEMPTS", 10)
OUTBOX_RETENTION_HOURS = env_int("OUTBOX_RETENTION_HOURS", 24)
OUTBOX_POLL_SECONDS = env_int("OUTBOX_POLL_SECONDS", 2)


# --- rate limiting (ADR-A41) ---------------------------------------------
#
# Fixed-window counters in the cache. Auth-shaped limits fail CLOSED when Redis
# is unavailable (refusing a login attempt is safe); browse-shaped limits fail
# OPEN (taking search down because a cache is missing is not).
RATELIMIT_ENABLED = env_bool("RATELIMIT_ENABLED", True)


# --- content security policy (ADR-A31) -----------------------------------
#
# Enforced, not report-only, and since P19 with no `unsafe-eval`: the app
# vendors the @alpinejs/csp build, which parses directive expressions instead of
# handing them to `new Function`. `script-src` is now `'self'` plus a
# per-request nonce and nothing else, so an injected inline script is blocked
# too. The obligation this leaves is on templates — see apps/common/csp.py for
# what an Alpine expression may contain.
CSP_REPORT_ONLY = env_bool("CSP_REPORT_ONLY", False)


# --- idempotency ---------------------------------------------------------

IDEMPOTENCY_TTL_SECONDS = env_int("IDEMPOTENCY_TTL_SECONDS", 24 * 60 * 60)


# --- security (tightened further in prod.py) -----------------------------

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"


# --- logging (structured, with a request id) -----------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "apps.common.logging.RequestIDFilter"},
    },
    "formatters": {
        "json": {
            "()": "apps.common.logging.JSONFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_id"],
            "formatter": "json",
        },
    },
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
    },
}
