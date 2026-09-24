import json
from datetime import datetime
from django.conf import settings
from django.utils import timezone
from redis import Redis
from redis.exceptions import LockError
from common.encryption import encrypt, decrypt
from .client import ConvayClient, ProviderAuthResult, ProviderError


def token_key(room):
    return f"convay:room:{room.pk}:v{room.credential_version}:access_token"


def invalidate(room):
    Redis.from_url(settings.REDIS_URL).delete(token_key(room))


def get_token(room, force=False, meeting_id=None):
    if not room.is_active:
        raise ProviderError("ROOM_INACTIVE")
    cache = Redis.from_url(
        settings.REDIS_URL, socket_timeout=3, socket_connect_timeout=3
    )
    key = token_key(room)
    try:
        with cache.lock(key + ":lock", timeout=60, blocking_timeout=10):
            raw = None if force else cache.get(key)
            if raw:
                try:
                    item = json.loads(decrypt(raw.decode()))
                    expiry = (
                        datetime.fromisoformat(item["expiry"])
                        if item["expiry"]
                        else None
                    )
                    if expiry is None or (expiry - timezone.now()).total_seconds() > 30:
                        return ProviderAuthResult(item["token"], expires_at=expiry)
                except (ValueError, KeyError):
                    cache.delete(key)
            client = ConvayClient(meeting_id=meeting_id)
            try:
                result = client.authenticate(room)
            except ProviderError as exc:
                type(room).objects.filter(pk=room.pk).update(
                    last_auth_at=timezone.now(),
                    last_auth_success=False,
                    last_error=exc.code,
                )
                raise
            finally:
                client.close()
            ttl = (
                min(300, int((result.expires_at - timezone.now()).total_seconds()) - 30)
                if result.expires_at
                else 60
            )
            if ttl <= 0:
                raise ProviderError("PROVIDER_TOKEN_EXPIRED")
            cache.setex(
                key,
                ttl,
                encrypt(
                    json.dumps(
                        {
                            "token": result.access_token,
                            "expiry": result.expires_at.isoformat()
                            if result.expires_at
                            else None,
                        }
                    )
                ),
            )
            type(room).objects.filter(pk=room.pk).update(
                last_auth_at=timezone.now(), last_auth_success=True, last_error=""
            )
            return result
    except LockError:
        raise ProviderError("PROVIDER_AUTH_BUSY") from None
