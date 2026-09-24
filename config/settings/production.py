from .base import *
from django.core.exceptions import ImproperlyConfigured

if not CREDENTIAL_ENCRYPTION_KEY or len(SECRET_KEY) < 50 or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "Production requires encryption key, strong secret, and explicit hosts."
    )
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False

if os.getenv("TRUST_PROXY_HTTPS", "false").lower() == "true":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if len(GATEWAY_JWT_SIGNING_KEY.encode()) < 32:
    raise ImproperlyConfigured(
        "Gateway JWT signing key must contain at least 32 bytes."
    )
