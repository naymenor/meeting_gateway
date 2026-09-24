import io
import json
import logging
from unittest.mock import patch

import httpx
import pytest
from django.test import RequestFactory

from apps.audit.models import AuditLog
from apps.convay.client import ConvayClient, ProviderError, ProviderAuthResult
from apps.convay.diagnostics import sanitize_payload, sanitize_response
from apps.meetings.services import provision
from common.exceptions import GatewayError
from common.logging import SafeJSONFormatter
from common.middleware import request_context


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,code,ambiguous",
    [
        (400, "PROVIDER_VALIDATION_ERROR", False),
        (401, "PROVIDER_AUTHENTICATION_ERROR", False),
        (403, "PROVIDER_AUTHORIZATION_ERROR", False),
        (404, "PROVIDER_ENDPOINT_ERROR", False),
        (422, "PROVIDER_VALIDATION_ERROR", False),
        (429, "PROVIDER_RATE_LIMITED", False),
        (500, "PROVIDER_STATE_UNKNOWN", True),
    ],
)
def test_http_classification_and_diagnostics(
    room, meeting, caplog, status, code, ambiguous
):
    client = ConvayClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                status,
                json={"message": "Invalid meetingType", "accessToken": "private-token"},
            )
        ),
        meeting_id=meeting.pk,
    )
    context = request_context.set({"request_id": "test-request"})
    try:
        with pytest.raises(ProviderError) as error:
            client.start_meeting(room, {}, "fake-token")
        assert error.value.code == code and error.value.ambiguous == ambiguous
        assert error.value.status_code == status
    finally:
        client.close()
        request_context.reset(context)
    record = next(
        r
        for r in caplog.records
        if r.name == "apps.convay.provider"
        and r.msg.get("event") == "convay.request_failed"
    )
    data = json.loads(SafeJSONFormatter().format(record))
    assert data["request_id"] == "test-request"
    assert data["operation"] == "convay.start_meeting"
    assert data["http_method"] == "POST"
    assert data["endpoint_path"] == "/services/vcmeetingsettings/api-user/start-meeting"
    assert data["upstream_status_code"] == status
    assert data["exception_class"] == "HTTPStatusError"
    assert data["room_public_id"] == room.public_id
    assert data["gateway_meeting_id"] == str(meeting.pk)
    assert data["elapsed_ms"] >= 0
    assert data["upstream_error"]["message"] == "Invalid meetingType"
    assert "private-token" not in caplog.text


@pytest.mark.django_db
@pytest.mark.parametrize(
    "exception,code,ambiguous",
    [
        (httpx.ConnectTimeout, "PROVIDER_UNAVAILABLE", False),
        (httpx.ConnectError, "PROVIDER_UNAVAILABLE", False),
        (httpx.ReadTimeout, "PROVIDER_STATE_UNKNOWN", True),
        (httpx.ReadError, "PROVIDER_STATE_UNKNOWN", True),
        (httpx.WriteTimeout, "PROVIDER_STATE_UNKNOWN", True),
    ],
)
def test_transport_classification(room, caplog, exception, code, ambiguous):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception("private transport exception details", request=request)

    client = ConvayClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as error:
            client.start_meeting(room, {}, "fake-token")
        assert error.value.code == code and error.value.ambiguous == ambiguous
        assert len(calls) == 1
    finally:
        client.close()
    record = next(
        r
        for r in caplog.records
        if r.name == "apps.convay.provider"
        and r.msg.get("event") == "convay.request_failed"
    )
    assert record.msg["exception_class"] == exception.__name__
    assert record.msg["upstream_status_code"] is None
    assert "private transport exception details" not in caplog.text


@pytest.mark.django_db
@pytest.mark.parametrize("second_status", [200, 401, 500])
def test_401_reauthentication_once(meeting, room, times, second_status):
    calls = []

    def handler(request):
        calls.append(request.headers["Authorization"])
        status = 401 if len(calls) == 1 else second_status
        return httpx.Response(
            status,
            json={
                "success": True,
                "data": {
                    "calendarId": "cal-test",
                    "meetingPanelAddress": "meet.convay.com",
                    "startMeetingUrl": "https://meet.convay.com/example?jwt=fake",
                },
            },
        )

    client = ConvayClient(transport=httpx.MockTransport(handler), meeting_id=meeting.pk)
    with (
        patch("apps.meetings.services.ConvayClient", return_value=client),
        patch(
            "apps.meetings.services.get_token",
            side_effect=[ProviderAuthResult("old"), ProviderAuthResult("new")],
        ) as auth,
        patch("apps.meetings.services.invalidate") as invalidate,
    ):
        values = {"roomId": room.public_id, "startAt": times[0], "endAt": times[1]}
        if second_status == 200:
            current, token = provision(meeting, values)
            assert current.status == "READY" and token.access_token == "new"
        else:
            with pytest.raises(GatewayError) as error:
                provision(meeting, values)
            assert error.value.default_code == (
                "PROVIDER_AUTHENTICATION_ERROR"
                if second_status == 401
                else "PROVIDER_STATE_UNKNOWN"
            )
        assert auth.call_count == 2
        assert auth.call_args.kwargs == {"force": True, "meeting_id": meeting.pk}
        assert invalidate.call_count == (2 if second_status == 401 else 1)
    assert calls == ["Bearer old", "Bearer new"]
    meeting.refresh_from_db()
    assert meeting.reservation_active == (second_status != 401)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,active",
    [(400, False), (403, False), (404, False), (422, False), (429, False), (500, True)],
)
def test_new_reservation_state_on_http_error(meeting, room, times, status, active):
    client = ConvayClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json={"message": "rejected"})
        )
    )
    with (
        patch("apps.meetings.services.ConvayClient", return_value=client),
        patch(
            "apps.meetings.services.get_token", return_value=ProviderAuthResult("fake")
        ),
    ):
        with pytest.raises(GatewayError):
            provision(
                meeting,
                {"roomId": room.public_id, "startAt": times[0], "endAt": times[1]},
            )
    meeting.refresh_from_db()
    assert meeting.reservation_active == active
    assert meeting.status == ("PROVIDER_STATE_UNKNOWN" if active else "FAILED")


def test_recursive_provider_redaction():
    body = {
        "message": "Invalid meetingType. password=leaked-password; Bearer leaked-bearer https://provider.invalid/start?jwt=leaked-url",
        "errors": [
            {
                "password": "private-password",
                "authorization": "private-authorization",
                "token": "private-token",
                "accessToken": "private-access",
                "access_token": "private-access-snake",
                "refreshToken": "private-refresh",
                "refresh_token": "private-refresh-snake",
                "startMeetingUrl": "https://provider.invalid/?jwt=private-url",
                "secret": "private-secret",
                "client_secret": "private-client-secret",
                "encryption_key": "private-key",
            },
            {"message": "private-access echoed; private-request-secret echoed"},
        ],
        "data": json.dumps({"accessToken": "private-embedded"}),
        "opaque": "private-opaque",
        "fieldErrors": [{"field": "title", "message": "NotBlank"}],
        "stackTrace": "com.example.ProviderException\n" * 10000,
    }
    safe = sanitize_response(httpx.Response(400, json=body), ["private-request-secret"])
    rendered = json.dumps(safe)
    assert "Invalid meetingType" in rendered
    assert "NotBlank" in rendered
    assert "stackTrace" not in rendered
    assert "private-" not in rendered and "leaked-" not in rendered
    assert "[REDACTED]" in rendered
    text_response = sanitize_response(
        httpx.Response(500, text="password=private-text-password"),
        ["private-text-password"],
    )
    assert text_response["text"] == "password=[REDACTED]"


def test_start_meeting_logs_exact_safe_payload_and_sanitized_response(
    room, meeting, caplog
):
    payload = {
        "title": "Physics",
        "meetingType": "instant",
        "preDefineHostEnabled": False,
        "config": {
            "MIC_OFF": True,
            "PASSWORD": False,
            "PRTCPNTS_LIST": "host",
            "accessToken": "must-not-log",
            "startMeetingUrl": "https://provider.invalid/start?jwt=must-not-log",
        },
    }
    response_body = {
        "success": False,
        "message": "invalid config",
        "accessToken": "response-secret",
    }
    client = ConvayClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(422, json=response_body)
        ),
        meeting_id=meeting.pk,
    )
    context = request_context.set({"request_id": "payload-request-id"})
    try:
        with pytest.raises(ProviderError):
            client.start_meeting(room, payload, "authorization-secret")
    finally:
        client.close()
        request_context.reset(context)

    request_record = next(
        r
        for r in caplog.records
        if r.msg.get("event") == "convay.start_meeting.request"
    )
    response_record = next(
        r
        for r in caplog.records
        if r.msg.get("event") == "convay.start_meeting.response"
    )
    request_log = json.loads(SafeJSONFormatter().format(request_record))
    response_log = json.loads(SafeJSONFormatter().format(response_record))
    assert request_log["gateway_meeting_id"] == str(meeting.pk)
    assert request_log["room_public_id"] == room.public_id
    assert (
        request_log["endpoint_path"]
        == "/services/vcmeetingsettings/api-user/start-meeting"
    )
    assert request_log["final_payload"] == {
        "title": "Physics",
        "meetingType": "instant",
        "preDefineHostEnabled": False,
        "config": {
            "MIC_OFF": True,
            "PASSWORD": False,
            "PRTCPNTS_LIST": "host",
            "accessToken": "[REDACTED]",
            "startMeetingUrl": "[REDACTED]",
        },
    }
    assert response_log["upstream_status_code"] == 422
    assert response_log["upstream_response"]["message"] == "invalid config"
    assert response_log["upstream_response"]["success"] is False
    assert response_log["upstream_response"]["accessToken"] == "[REDACTED]"
    rendered = json.dumps([request_log, response_log])
    for secret in ("must-not-log", "response-secret", "authorization-secret"):
        assert secret not in rendered
    assert (
        request_log["request_id"] == response_log["request_id"] == "payload-request-id"
    )


def test_payload_sanitizer_keeps_documented_boolean_password_flag():
    assert sanitize_payload(
        {
            "config": {"PASSWORD": True},
            "password": "credential",
            "authorization": "Bearer secret",
            "refresh_token": "secret",
        }
    ) == {
        "config": {"PASSWORD": True},
        "password": "[REDACTED]",
        "authorization": "[REDACTED]",
        "refresh_token": "[REDACTED]",
    }


@pytest.mark.django_db
def test_request_id_in_provider_response_django_log_and_audit(
    api, meeting, room, times
):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeJSONFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    client = ConvayClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                422,
                json={
                    "message": "Invalid field",
                    "password": "private-upstream-password",
                },
            )
        ),
        meeting_id=meeting.pk,
    )
    try:
        with (
            patch("apps.meetings.services.ConvayClient", return_value=client),
            patch(
                "apps.meetings.services.get_token",
                return_value=ProviderAuthResult("private-access"),
            ),
        ):
            response = api.post(
                f"/api/v1/meetings/{meeting.pk}/create/",
                {
                    "roomId": room.public_id,
                    "startAt": times[0].isoformat(),
                    "endAt": times[1].isoformat(),
                },
                format="json",
            )
    finally:
        root.removeHandler(handler)
    request_id = response["X-Request-ID"]
    assert response.status_code == 422 and response.data["requestId"] == request_id
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    for logger in ["apps.convay.provider", "django.request", "gateway.http"]:
        rows = [r for r in records if r["logger"] == logger]
        assert rows and all(r["request_id"] == request_id for r in rows)
    event = AuditLog.objects.get(action="provider_error", object_id=str(meeting.pk))
    assert event.request_id == request_id
    assert event.metadata["upstream_status_code"] == 422
    assert "private-" not in stream.getvalue()
    assert request_context.get() == {}
    replay = api.post(
        f"/api/v1/meetings/{meeting.pk}/create/",
        {
            "roomId": room.public_id,
            "startAt": times[0].isoformat(),
            "endAt": times[1].isoformat(),
        },
        format="json",
    )
    assert replay.status_code == 409
    assert replay.data["code"] == "INVALID_STATE"
    assert replay.data["requestId"] == replay["X-Request-ID"] != request_id


def test_formatter_falls_back_to_request_after_middleware_cleanup():
    request = RequestFactory().get("/missing/?token=private-query")
    request.request_id = "preserved-request-id"
    record = logging.LogRecord(
        "django.request", logging.ERROR, "", 0, "unsafe private-message", (), None
    )
    record.request = request
    record.status_code = 404
    data = json.loads(SafeJSONFormatter().format(record))
    assert data["request_id"] == "preserved-request-id"
    assert data["event"] == "http.request_failed"
    assert "private-" not in json.dumps(data)


def test_early_https_redirect_has_request_id(client, settings):
    settings.SECURE_SSL_REDIRECT = True
    response = client.get("/health/live/")
    assert response.status_code == 301 and response["X-Request-ID"]


@pytest.mark.django_db
def test_existing_unknown_meeting_is_not_retried_or_released(meeting, room, times):
    from apps.rooms.services import RoomAllocator
    from apps.meetings.models import Meeting

    reserved = RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    Meeting.objects.filter(pk=meeting.pk).update(status="PROVIDER_STATE_UNKNOWN")
    with patch("apps.meetings.services.ConvayClient") as provider:
        with pytest.raises(GatewayError):
            provision(
                meeting,
                {"roomId": room.public_id, "startAt": times[0], "endAt": times[1]},
            )
        provider.assert_not_called()
    meeting.refresh_from_db()
    assert meeting.status == "PROVIDER_STATE_UNKNOWN" and meeting.reservation_active
    assert meeting.reservation == reserved.reservation


@pytest.mark.django_db
def test_401_refreshes_through_real_auth_adapter(meeting, room, times, caplog):
    original_client = httpx.Client
    operations = []

    def handler(request):
        if request.url.path.endswith("/authenticate"):
            operations.append("auth")
            assert json.loads(request.content)["password"] == "fake-provider-password"
            token = "opaque-old" if operations.count("auth") == 1 else "opaque-new"
            return httpx.Response(200, json={"success": True, "data": token})
        operations.append("start")
        if operations.count("start") == 1:
            assert request.headers["Authorization"] == "Bearer opaque-old"
            return httpx.Response(
                401,
                json={
                    "message": "Bearer opaque-old rejected",
                    "accessToken": "opaque-old",
                },
            )
        assert request.headers["Authorization"] == "Bearer opaque-new"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "calendarId": "cal-new",
                    "meetingPanelAddress": "meet.convay.com",
                    "startMeetingUrl": "https://meet.convay.com/example?jwt=fake",
                },
            },
        )

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(**kwargs)

    with patch("apps.convay.client.httpx.Client", side_effect=factory):
        result, token = provision(
            meeting, {"roomId": room.public_id, "startAt": times[0], "endAt": times[1]}
        )
    assert operations == ["auth", "start", "auth", "start"]
    assert result.status == "READY" and token.access_token == "opaque-new"
    for secret in ["opaque-old", "opaque-new", "fake-provider-password"]:
        assert secret not in caplog.text


@pytest.mark.django_db
def test_auth_failure_logs_safely_and_keeps_meeting_context(room, meeting, caplog):
    client = ConvayClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                403,
                json={
                    "message": "fake-provider-password rejected",
                    "data": {"password": "fake-provider-password"},
                },
            )
        ),
        meeting_id=meeting.pk,
    )
    try:
        with pytest.raises(ProviderError):
            client.authenticate(room)
    finally:
        client.close()
    record = next(r for r in caplog.records if r.name == "apps.convay.provider")
    assert record.msg["operation"] == "convay.authenticate"
    assert record.msg["gateway_meeting_id"] == str(meeting.pk)
    assert "fake-provider-password" not in caplog.text


@pytest.mark.django_db
def test_known_creation_with_invalid_local_url_preserves_identifiers(
    meeting, room, times
):
    body = {
        "calendarId": "known-calendar-id",
        "meetingPanelAddress": "another.convay.com",
        "startMeetingUrl": "http://meet.convay.com/not-https?jwt=fake",
    }
    provider = ConvayClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        meeting_id=meeting.pk,
    )
    with (
        patch("apps.meetings.services.ConvayClient", return_value=provider),
        patch(
            "apps.meetings.services.get_token",
            return_value=ProviderAuthResult("fake-access"),
        ),
    ):
        with pytest.raises(GatewayError) as error:
            provision(
                meeting,
                {
                    "roomId": room.public_id,
                    "startAt": times[0],
                    "endAt": times[1],
                },
            )
    assert error.value.default_code == "PROVIDER_RESPONSE_INVALID"
    meeting.refresh_from_db()
    assert meeting.status == "PROVIDER_RESPONSE_INVALID"
    assert meeting.reservation_active
    assert meeting.provider_calendar_id == "known-calendar-id"
    assert meeting.provider_panel_address == "another.convay.com"
    assert meeting.encrypted_provider_start_url is None
