import pytest
from unittest.mock import patch
from rest_framework.test import APIClient
from drf_spectacular.generators import SchemaGenerator

from apps.convay.client import ProviderAuthResult, ProviderMeetingResult
from apps.integrations.tokens import issue_token


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
    url = f"/api/v1/meetings/{meeting.pk}/create/"
    payload = {
        "roomId": room.public_id,
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
    registration = paths["/api/v1/meetings/"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert registration["$ref"].endswith("/Registration")
    registration_schema = schema["components"]["schemas"]["Registration"]
    assert "meetingTitle" in registration_schema["properties"]

    create_path = next(
        path
        for path in paths
        if path.startswith("/api/v1/meetings/")
        and path.endswith("/create/")
    )
    create_schema = paths[create_path]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert create_schema["$ref"].endswith("/Booking")
    booking = schema["components"]["schemas"]["Booking"]
    assert set(booking["properties"]) == {"roomId", "startAt", "endAt"}
    assert {"startAt", "endAt"} <= set(booking["required"])
    assert not {"teacher", "subject", "batch", "class"} & set(booking["properties"])

    assert any(path.endswith("/convay-token/") for path in paths)
    assert "/api/v1/rooms/availability/" in paths
    assert "/health/live/" in paths
    assert "/health/ready/" in paths
    mutation_paths = [
        "/api/v1/meetings/",
        create_path,
        next(path for path in paths if path.endswith("/cancel/")),
        next(path for path in paths if path.endswith("/convay-token/")),
    ]
    for path in mutation_paths:
        parameters = paths[path]["post"].get("parameters", [])
        assert all(item["name"].lower() != "idempotency-key" for item in parameters)
    assert {"200", "201"} <= set(paths["/api/v1/meetings/"]["post"]["responses"])
    assert {"200", "201", "409", "422", "502", "503"} <= set(
        paths[create_path]["post"]["responses"]
    )
