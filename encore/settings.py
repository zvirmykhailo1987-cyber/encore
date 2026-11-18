"""Settings are read from the environment. No secret has a usable default."""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("ENCORE_DEBUG", "0") == "1"
SECRET_KEY = os.environ.get("ENCORE_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("ENCORE_SECRET_KEY must be set when ENCORE_DEBUG is not 1")
    SECRET_KEY = "local-debug-only"

# Empty means write endpoints are open on a debug machine and closed everywhere else.
ENCORE_OPERATOR_TOKEN = os.environ.get("ENCORE_OPERATOR_TOKEN", "")

ALLOWED_HOSTS = [h for h in os.environ.get("ENCORE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "rest_framework",
    "crm",
    "imports",
    "ticketing",
    "console",
]

MIDDLEWARE = [
    "encore.middleware.RequestLog",
    "encore.middleware.OperationalEndpoints",
    "django.middleware.security.SecurityMiddleware",
    "encore.middleware.ContentSecurityPolicy",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]

X_FRAME_OPTIONS = "DENY"
if os.environ.get("ENCORE_HTTPS", "0") == "1":
    # Only behind a TLS proxy that sets this header and strips it from client requests.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

ROOT_URLCONF = "encore.urls"
WSGI_APPLICATION = "encore.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]

DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("DATABASE_URL", "postgres://encore:encore@127.0.0.1:5433/encore"),
        conn_max_age=60,
    )
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/1"),
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "encore.logs.JsonFormatter"}},
    "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["stdout"], "level": os.environ.get("ENCORE_LOG_LEVEL", "INFO")},
}
# Requests slower than this are logged. Logging every one would cost more than it tells at peak traffic.
ENCORE_SLOW_REQUEST_MS = int(os.environ.get("ENCORE_SLOW_REQUEST_MS", "1000"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6380/0")
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_IGNORE_RESULT = True
# Imports have their own queue and workers, so a long import cannot hold up ticket sync or check-in delivery.
CELERY_TASK_ROUTES = {"imports.run_import": {"queue": "imports"}, "imports.reconcile": {"queue": "imports"}}
# Redis hands a long unacknowledged task to another worker after this time, one hour by default. Our lease makes such
# a second delivery harmless, so the timeout only needs to be long enough not to create needless ones.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 43200}
CELERY_BEAT_SCHEDULE = {
    "reap-stale-import-runs": {"task": "imports.reap_stale_runs", "schedule": 15.0},
    "sync-tickets": {"task": "ticketing.sync_tickets", "schedule": 10.0},
    "flush-outbound-checkins": {"task": "ticketing.flush_outbox", "schedule": 5.0},
}

# Import pipeline
ENCORE_IMPORT_BATCH_SIZE = int(os.environ.get("ENCORE_IMPORT_BATCH_SIZE", "5000"))
ENCORE_IMPORT_SOURCE_DIR = Path(os.environ.get("ENCORE_IMPORT_SOURCE_DIR", BASE_DIR / "data"))
ENCORE_IMPORT_HEARTBEAT_SECONDS = float(os.environ.get("ENCORE_IMPORT_HEARTBEAT_SECONDS", "5"))
ENCORE_IMPORT_STALE_SECONDS = int(os.environ.get("ENCORE_IMPORT_STALE_SECONDS", "30"))
# A pause after every batch, to leave room for live traffic while an organizer has an on-sale running.
ENCORE_IMPORT_BATCH_PAUSE_MS = int(os.environ.get("ENCORE_IMPORT_BATCH_PAUSE_MS", "0"))
# On Kubernetes a worker pod can vanish at any time, so a run should restart itself. A limit stops a crash loop.
ENCORE_IMPORT_AUTO_RESUME = os.environ.get("ENCORE_IMPORT_AUTO_RESUME", "0") == "1"
ENCORE_IMPORT_MAX_AUTO_RESUMES = int(os.environ.get("ENCORE_IMPORT_MAX_AUTO_RESUMES", "5"))

# Ticketing connector
TICKETING_BASE_URL = os.environ.get("TICKETING_BASE_URL", "http://localhost:8099")
TICKETING_WEBHOOK_SECRET = os.environ.get(
    "TICKETING_WEBHOOK_SECRET", "local-demo-webhook-secret" if DEBUG else ""
)
TICKETING_IS_MOCK = os.environ.get("TICKETING_IS_MOCK", "1") == "1"
TICKETING_RATE_PER_SECOND = float(os.environ.get("TICKETING_RATE_PER_SECOND", "20"))
