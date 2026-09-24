import ipaddress
import uuid

import jwt
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from redis import Redis
from rest_framework.exceptions import AuthenticationFailed, Throttled

from .models import IntegrationClient, SCOPES

DUMMY_HASH = make_password("unusable-dummy-secret")


class MachineAuthenticationError(AuthenticationFailed):
    def __init__(self, code):
        self.default_code = code
        super().__init__("Machine authentication failed.", code=code)


def ip_allowed(client, remote):
    if not client.ip_allowlist:
        return True
    try:
        address = ipaddress.ip_address(remote)
        return any(address in ipaddress.ip_network(n) for n in client.ip_allowlist)
    except (ValueError, TypeError):
        return False


def throttle_exchange(remote):
    cache = Redis.from_url(
        settings.REDIS_URL, socket_timeout=3, socket_connect_timeout=3
    )
    bucket = f"gateway-token-limit:{remote}:{int(timezone.now().timestamp()) // 60}"
    with cache.pipeline() as pipe:
        count, _ = pipe.incr(bucket).expire(bucket, 120).execute()
    if count > 60:
        raise Throttled(wait=60)


def authenticate_credentials(client_id, client_secret, remote):
    try:
        identifier = uuid.UUID(client_id)
    except (ValueError, TypeError, AttributeError):
        identifier = None
    client = (
        IntegrationClient.objects.filter(client_id=identifier).first()
        if identifier
        else None
    )
    valid = check_password(client_secret, client.secret_hash if client else DUMMY_HASH)
    if (
        not client
        or not valid
        or not client.is_active
        or not ip_allowed(client, remote)
    ):
        raise MachineAuthenticationError("INVALID_CLIENT")
    return client


def issue_token(client):
    issued = int(timezone.now().timestamp())
    claims = {
        "sub": str(client.pk),
        "client_id": str(client.client_id),
        "scopes": sorted(set(client.scopes)),
        "iat": issued,
        "exp": issued + settings.GATEWAY_ACCESS_TOKEN_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
        "ver": str(client.token_version),
        "iss": settings.GATEWAY_JWT_ISSUER,
        "aud": settings.GATEWAY_JWT_AUDIENCE,
    }
    return jwt.encode(claims, settings.GATEWAY_JWT_SIGNING_KEY, algorithm="HS256")


def validate_token(token, remote):
    try:
        claims = jwt.decode(
            token,
            settings.GATEWAY_JWT_SIGNING_KEY,
            algorithms=["HS256"],
            issuer=settings.GATEWAY_JWT_ISSUER,
            audience=settings.GATEWAY_JWT_AUDIENCE,
            options={
                "require": [
                    "sub",
                    "client_id",
                    "scopes",
                    "iat",
                    "exp",
                    "jti",
                    "ver",
                    "iss",
                    "aud",
                ]
            },
        )
        identifier = uuid.UUID(claims["sub"])
        uuid.UUID(claims["client_id"])
        uuid.UUID(claims["jti"])
        uuid.UUID(claims["ver"])
        if (
            type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or claims["exp"] <= claims["iat"]
            or not isinstance(claims["scopes"], list)
            or any(not isinstance(s, str) or s not in SCOPES for s in claims["scopes"])
        ):
            raise ValueError
    except jwt.ExpiredSignatureError:
        raise MachineAuthenticationError("TOKEN_EXPIRED") from None
    except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
        raise MachineAuthenticationError("INVALID_TOKEN") from None
    client = IntegrationClient.objects.filter(pk=identifier, is_active=True).first()
    if (
        not client
        or str(client.client_id) != claims["client_id"]
        or str(client.token_version) != claims["ver"]
        or not ip_allowed(client, remote)
    ):
        raise MachineAuthenticationError("INVALID_TOKEN")
    # Scope removals take effect immediately; additions require a new token.
    client.scopes = sorted(set(client.scopes).intersection(claims["scopes"]))
    IntegrationClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
    return client, claims
