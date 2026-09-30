import pytest
from unittest.mock import patch
from rest_framework.test import APIClient
from drf_spectacular.generators import SchemaGenerator

from apps.convay.client import ProviderAuthResult, ProviderMeetingResult
from apps.integrations.tokens import issue_token
from tests.test_api import PAYLOAD


@pytest.mark.django_db
@pytest.mark.parametrize(
    "initial_scopes", [["meeting:write"], ["meeting:write", "meeting:token"]]
)
def test_create_with_write_scope_and_safe_replay(
    client_account, meeting, room, times, initial_scopes
):
    client_account.scopes = initial_scopes
    client_account.save()
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION="Bearer " + issue_token(client_account))
    url = "/api/v1/meetings/"
    payload = {
        **PAYLOAD,
        "startAt": times[0].isoformat(),
        "endAt": times[1].isoformat(),
    }
    provider = ProviderMeetingResult(
        calendar_id="fake-calendar",
        meeting_panel_address="meet.convay.com",
        start_meeting_url="https://meet.convay.com/start?token=fake",
    )
    with (
        patch(
            "apps.meetings.services.get_token",
            return_value=ProviderAuthResult("fake-convay-token"),
        ),
        patch(
            "apps.meetings.services.ConvayClient.start_meeting", return_value=provider
        ) as upstream,
    ):
        response = api.post(url, payload, format="json")
        assert response.status_code == 201, response.data
        assert ("authorization" in response.data["data"]["convay"]) == (
            "meeting:token" in initial_scopes
        )
        client_account.scopes = ["meeting:write"]
        client_account.save()
        replay = api.post(url, payload, format="json")
        assert replay.status_code == 200
        assert "authorization" not in replay.data["data"]["convay"]
        assert "startMeetingUrl" not in replay.data["data"]["convay"]
        assert upstream.call_count == 1


def test_openapi_has_bearer_and_public_exchange():
    schema = SchemaGenerator().get_schema(public=True)
    paths = schema["paths"]
    scheme = schema["components"]["securitySchemes"]["GatewayBearer"]
    assert scheme["scheme"] == "bearer" and scheme["bearerFormat"] == "JWT"
    assert "/api/v1/auth/token/" in paths
    assert {"GatewayBearer": []} in paths["/api/v1/meetings/"]["get"]["security"]
    assert "security" not in paths["/api/v1/auth/token/"]["post"]
    operation = paths["/api/v1/meetings/"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    creation = schema["components"]["schemas"][request_schema["$ref"].split("/")[-1]]
    assert set(creation["properties"]) == {"meetingTitle", "teacher", "batch", "class", "startAt", "endAt"}
    assert set(creation["required"]) == set(creation["properties"])
    assert "subject" not in str(schema)
    assert not any(path.endswith(("/create/", "/cancel/")) for path in paths)
    assert any(path.endswith("/convay-token/") for path in paths)
    assert "/api/v1/rooms/availability/" in paths
    assert "/health/live/" in paths and "/health/ready/" in paths
    ids = [op["operationId"] for methods in paths.values() for op in methods.values() if isinstance(op, dict) and "operationId" in op]
    assert len(ids) == len(set(ids))
    assert {"create_meeting", "list_meetings", "retrieve_meeting", "check_meeting_availability", "get_convay_token", "create_gateway_token"} <= set(ids)
    assert {"200", "201", "409", "422", "502", "503"} <= set(operation["responses"])
    for methods in paths.values():
        for op in methods.values():
            assert all(p["name"].lower() != "idempotency-key" for p in op.get("parameters", []))

    assert not any(value in str(schema) for value in ("roomId", "roomInfo", "roomName", "room_id"))
    availability = paths["/api/v1/rooms/availability/"]["get"]
    assert {p["name"] for p in availability["parameters"]} == {"start_at", "end_at"}
    assert all(p["required"] for p in availability["parameters"])
