import os
from pathlib import Path
from urllib.parse import urlsplit
import dj_database_url

BASE_DIR = Path(__file__).resolve().parents[2]
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost").split(",")
CSRF_TRUSTED_ORIGINS = list(
    filter(None, os.getenv("CSRF_TRUSTED_ORIGINS", "").split(","))
)
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "drf_spectacular",
    "apps.accounts",
    "apps.integrations",
    "apps.rooms",
    "apps.meetings",
    "apps.convay",
    "apps.audit",
]
MIDDLEWARE = [
    "common.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
DATABASES = {
    "default": dj_database_url.parse(os.environ["DATABASE_URL"], conn_max_age=60)
}
REDIS_URL = os.environ["REDIS_URL"]
CELERY_BROKER_URL = REDIS_URL
CELERY_TASK_IGNORE_RESULT = True
CELERY_BEAT_SCHEDULE = {
    "flag-stale-provisioning": {
        "task": "apps.meetings.tasks.flag_stale_provisioning",
        "schedule": 60.0,
    }
}
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}
USE_TZ = True
TIME_ZONE = "Asia/Dhaka"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
]
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.integrations.authentication.ClientAuthentication"
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "common.exceptions.exception_handler",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Meeting Gateway",
    "VERSION": "1.0.0",
    "DESCRIPTION": "Trusted backend API. Exchange client credentials at /api/v1/auth/token/ for a Gateway Bearer JWT over HTTPS. Scopes and ownership enforced; Convay tokens may be account scoped.",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
    "SERVE_AUTHENTICATION": ["rest_framework.authentication.SessionAuthentication"],
}
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
CREDENTIAL_ENCRYPTION_KEY_VERSION = os.getenv("CREDENTIAL_ENCRYPTION_KEY_VERSION", "1")
CONVAY_BASE_URL = os.getenv("CONVAY_BASE_URL", "https://convay.com")
_convay_base_host = urlsplit(CONVAY_BASE_URL).hostname
CONVAY_TRUSTED_HOST_SUFFIXES = tuple(
    host.strip().lower().lstrip(".")
    for host in os.getenv(
        "CONVAY_TRUSTED_HOST_SUFFIXES", _convay_base_host or ""
    ).split(",")
    if host.strip()
)
CONVAY_AUTH_PATH = os.getenv(
    "CONVAY_AUTH_PATH", "/services/vcmeetingsettings/user/authenticate"
)
CONVAY_START_PATH = os.getenv(
    "CONVAY_START_PATH", "/services/vcmeetingsettings/api-user/start-meeting"
)
CONVAY_CONNECT_TIMEOUT = float(os.getenv("CONVAY_CONNECT_TIMEOUT", "5"))
CONVAY_READ_TIMEOUT = float(os.getenv("CONVAY_READ_TIMEOUT", "30"))
DEFAULT_PROVIDER_MEETING_TYPE = os.getenv("DEFAULT_PROVIDER_MEETING_TYPE", "instant")
ROOM_BUFFER_BEFORE_MINUTES = int(os.getenv("ROOM_BUFFER_BEFORE_MINUTES", "0"))
ROOM_BUFFER_AFTER_MINUTES = int(os.getenv("ROOM_BUFFER_AFTER_MINUTES", "0"))
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "common.logging.SafeJSONFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"httpx": {"level": "WARNING"}, "httpcore": {"level": "WARNING"}},
}

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

if ROOM_BUFFER_BEFORE_MINUTES < 0 or ROOM_BUFFER_AFTER_MINUTES < 0:
    raise ValueError("Room buffers must not be negative.")
if not CONVAY_BASE_URL.startswith("https://"):
    raise ValueError("Convay requires an HTTPS base URL.")
if not CONVAY_TRUSTED_HOST_SUFFIXES:
    raise ValueError("At least one trusted Convay host suffix is required.")

CELERY_WORKER_HIJACK_ROOT_LOGGER = False

GATEWAY_ACCESS_TOKEN_TTL_SECONDS = int(
    os.getenv("GATEWAY_ACCESS_TOKEN_TTL_SECONDS", "3600")
)
GATEWAY_JWT_SIGNING_KEY = os.getenv("GATEWAY_JWT_SIGNING_KEY") or SECRET_KEY
GATEWAY_JWT_ISSUER = "meeting-gateway"
GATEWAY_JWT_AUDIENCE = "meeting-gateway-api"
if GATEWAY_ACCESS_TOKEN_TTL_SECONDS <= 0:
    raise ValueError("Gateway access token TTL must be positive.")
