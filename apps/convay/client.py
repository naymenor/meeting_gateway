import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
import httpx
from django.conf import settings
from common.encryption import decrypt
from common.middleware import request_context
from .diagnostics import (
    endpoint_path,
    sanitize_payload,
    sanitize_response,
    sensitive_values,
)

logger = logging.getLogger("apps.convay.provider")


class ProviderError(Exception):
    def __init__(
        self,
        code,
        ambiguous=False,
        status_code=None,
        known_created=False,
        provider_result=None,
    ):
        self.code, self.ambiguous = code, ambiguous
        self.status_code = status_code
        self.known_created = known_created
        self.provider_result = provider_result
        super().__init__(code)


@dataclass
class ProviderAuthResult:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None


@dataclass
class ProviderMeetingResult:
    calendar_id: str
    meeting_panel_address: str | None
    start_meeting_url: str | None
    unique_id: str | None = None


def normalize_auth(body):
    try:
        if body.get("success") is not True:
            raise ValueError
        data = body["data"]
        if isinstance(data, str) and data.lstrip().startswith("{"):
            data = json.loads(data)
        token = data.get("accessToken") if isinstance(data, dict) else data
        if not isinstance(token, str) or not token or len(token) > 32768:
            raise ValueError
        refresh = data.get("refreshToken") if isinstance(data, dict) else None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProviderError("MALFORMED_AUTH_RESPONSE") from None
    expiry = None
    try:
        segment = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        )
        expiry = datetime.fromtimestamp(float(claims["exp"]), tz=timezone.utc)
    except (
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        OverflowError,
        OSError,
        AttributeError,
    ):
        pass
    # Unverified exp is only a conservative cache hint, never authentication proof.
    return ProviderAuthResult(token, refresh, expiry)


class ConvayClient:
    def __init__(self, transport=None, meeting_id=None):
        self.meeting_id = str(meeting_id) if meeting_id is not None else None
        self.http = httpx.Client(
            base_url=settings.CONVAY_BASE_URL,
            timeout=httpx.Timeout(
                settings.CONVAY_READ_TIMEOUT, connect=settings.CONVAY_CONNECT_TIMEOUT
            ),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
            transport=transport,
        )

    def close(self):
        self.http.close()

    def _post(self, path, payload, room, operation, parser, token=None, creation=False):
        started = time.monotonic()
        response = None
        secrets = sensitive_values(payload) + [
            token,
            room.username,
            settings.CREDENTIAL_ENCRYPTION_KEY,
            settings.SECRET_KEY,
            settings.GATEWAY_JWT_SIGNING_KEY,
        ]

        def fail(error, exception_class):
            logger.warning(
                {
                    "event": "convay.request_failed",
                    "operation": operation,
                    "http_method": "POST",
                    "endpoint_path": endpoint_path(path),
                    "upstream_status_code": response.status_code
                    if response is not None
                    else None,
                    "exception_class": exception_class,
                    "error_code": error.code,
                    "outcome_unknown": error.ambiguous,
                    "upstream_error": sanitize_response(response, secrets),
                    "room_public_id": room.public_id,
                    "gateway_meeting_id": self.meeting_id,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                },
                extra={"request_id": request_context.get().get("request_id")},
            )
            raise error from None

        try:
            if operation == "convay.start_meeting":
                logger.info(
                    {
                        "event": "convay.start_meeting.request",
                        "operation": operation,
                        "gateway_meeting_id": self.meeting_id,
                        "room_public_id": room.public_id,
                        "http_method": "POST",
                        "endpoint_path": endpoint_path(path),
                        "final_payload": sanitize_payload(payload, secrets),
                    },
                    extra={"request_id": request_context.get().get("request_id")},
                )
            response = self.http.post(
                path,
                json=payload,
                headers={"Authorization": "Bearer " + token} if token else {},
            )
        except (
            httpx.ConnectTimeout,
            httpx.ConnectError,
            httpx.PoolTimeout,
            httpx.UnsupportedProtocol,
        ) as exc:
            fail(ProviderError("PROVIDER_UNAVAILABLE"), type(exc).__name__)
        except httpx.RequestError as exc:
            fail(
                ProviderError(
                    "PROVIDER_STATE_UNKNOWN" if creation else "PROVIDER_UNAVAILABLE",
                    ambiguous=creation,
                ),
                type(exc).__name__,
            )
        if operation == "convay.start_meeting":
            logger.info(
                {
                    "event": "convay.start_meeting.response",
                    "operation": operation,
                    "gateway_meeting_id": self.meeting_id,
                    "room_public_id": room.public_id,
                    "http_method": "POST",
                    "endpoint_path": endpoint_path(path),
                    "upstream_status_code": response.status_code,
                    "upstream_response": sanitize_response(
                        response, secrets, preserve_safe_fields=True
                    ),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                },
                extra={"request_id": request_context.get().get("request_id")},
            )
        if not response.is_success:
            status = response.status_code
            codes = {
                400: "PROVIDER_VALIDATION_ERROR",
                422: "PROVIDER_VALIDATION_ERROR",
                401: "PROVIDER_AUTHENTICATION_ERROR",
                403: "PROVIDER_AUTHORIZATION_ERROR",
                404: "PROVIDER_ENDPOINT_ERROR",
                429: "PROVIDER_RATE_LIMITED",
            }
            ambiguous = creation and (
                status >= 500 or status == 408 or 300 <= status < 400
            )
            code = codes.get(
                status,
                "PROVIDER_STATE_UNKNOWN"
                if ambiguous
                else "PROVIDER_UNAVAILABLE"
                if status >= 500
                else "PROVIDER_HTTP_ERROR",
            )
            fail(
                ProviderError(code, ambiguous=ambiguous, status_code=status),
                "HTTPStatusError",
            )
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            return parser(body)
        except ProviderError as exc:
            exc.status_code = response.status_code
            fail(exc, type(exc).__name__)
        except (ValueError, TypeError, RecursionError) as exc:
            fail(
                ProviderError(
                    "PROVIDER_STATE_UNKNOWN"
                    if creation
                    else "MALFORMED_PROVIDER_RESPONSE",
                    ambiguous=creation,
                    status_code=response.status_code,
                ),
                type(exc).__name__,
            )

    def authenticate(self, room):
        return self._post(
            settings.CONVAY_AUTH_PATH,
            {"username": room.username, "password": decrypt(room.encrypted_password)},
            room,
            "convay.authenticate",
            normalize_auth,
        )

    def start_meeting(self, room, payload, token):
        return self._post(
            settings.CONVAY_START_PATH,
            payload,
            room,
            "convay.start_meeting",
            self._parse_meeting,
            token=token,
            creation=True,
        )

    @staticmethod
    def _parse_meeting(body):
        if body.get("success") is False:
            raise ProviderError("PROVIDER_VALIDATION_ERROR")
        data = body.get("data", body)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                raise ProviderError(
                    "MALFORMED_CREATION_RESPONSE", ambiguous=True
                ) from None
        if not isinstance(data, dict):
            raise ProviderError("MALFORMED_CREATION_RESPONSE", ambiguous=True)

        raw_calendar = data.get("calendarId")
        if not isinstance(raw_calendar, (str, int)):
            raise ProviderError("MALFORMED_CREATION_RESPONSE", ambiguous=True)
        calendar_id = str(raw_calendar).strip()
        if not calendar_id or len(calendar_id) > 200:
            raise ProviderError("MALFORMED_CREATION_RESPONSE", ambiguous=True)

        panel = data.get("meetingPanelAddress")
        panel = panel.strip() if isinstance(panel, str) else None
        unique = data.get("uniqueId")
        unique_id = str(unique).strip() if isinstance(unique, (str, int)) else None
        partial = ProviderMeetingResult(
            calendar_id=calendar_id,
            meeting_panel_address=panel,
            start_meeting_url=None,
            unique_id=unique_id,
        )

        if (
            not panel
            or len(panel) > 2000
            or any(ord(character) < 32 for character in panel)
        ):
            raise ProviderError(
                "PROVIDER_RESPONSE_INVALID",
                known_created=True,
                provider_result=partial,
            )
        if unique is not None and (not unique_id or len(unique_id) > 200):
            raise ProviderError(
                "PROVIDER_RESPONSE_INVALID",
                known_created=True,
                provider_result=partial,
            )

        start_url = data.get("startMeetingUrl")
        if not isinstance(start_url, str):
            raise ProviderError(
                "PROVIDER_RESPONSE_INVALID",
                known_created=True,
                provider_result=partial,
            )
        start_url = start_url.strip()
        parsed_start = urlsplit(start_url)
        trusted_suffixes = settings.CONVAY_TRUSTED_HOST_SUFFIXES
        trusted_host = parsed_start.hostname and any(
            parsed_start.hostname == suffix
            or parsed_start.hostname.endswith("." + suffix)
            for suffix in trusted_suffixes
        )
        if (
            not start_url
            or len(start_url) > 16384
            or parsed_start.scheme.lower() != "https"
            or not parsed_start.netloc
            or parsed_start.username is not None
            or parsed_start.password is not None
            or not trusted_host
        ):
            raise ProviderError(
                "PROVIDER_RESPONSE_INVALID",
                known_created=True,
                provider_result=partial,
            )

        return ProviderMeetingResult(
            calendar_id=calendar_id,
            meeting_panel_address=panel,
            start_meeting_url=start_url,
            unique_id=unique_id,
        )
