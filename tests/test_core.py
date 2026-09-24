import base64
import json
from datetime import timedelta
from unittest.mock import patch
import httpx
import pytest
from cryptography.fernet import InvalidToken
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.contrib.auth.models import User
from apps.rooms.services import RoomAllocator, availability
from apps.rooms.models import Room, MeetingConfigPreset
from apps.rooms.configuration import validate_config, resolve_config
from apps.rooms.admin import RoomAdmin, RoomForm
from apps.meetings.models import Meeting
from apps.meetings.services import provision, cancel
from apps.convay.client import (
    normalize_auth,
    ConvayClient,
    ProviderError,
    ProviderAuthResult,
)
from common.encryption import encrypt, decrypt
from common.exceptions import GatewayError
from common.logging import redact


def test_encryption():
    value = encrypt("fake-private-password")
    assert "fake-private-password" not in value
    assert decrypt(value) == "fake-private-password"
    with pytest.raises(InvalidToken):
        decrypt(value[:-5] + "AAAAA")


@pytest.mark.django_db
def test_room_encrypted(room):
    assert room.encrypted_password != "fake-provider-password"
    assert decrypt(room.encrypted_password) == "fake-provider-password"


@pytest.mark.django_db
def test_specific_adjacent_overlap(meeting, room, times, client_account):
    start, end = times
    reserved = RoomAllocator.reserve_specific_room(meeting, room.public_id, start, end)
    assert reserved.status == "RESERVED"
    second = Meeting.objects.create(
        integration_client=client_account,
        external_class_id="CLS-2",
        class_date=start.date(),
    )
    with pytest.raises(GatewayError, match="no longer available"):
        RoomAllocator.reserve_specific_room(
            second, room.public_id, start + timedelta(minutes=30), end
        )
    RoomAllocator.reserve_specific_room(
        second, room.public_id, end, end + timedelta(hours=1)
    )
    # Direct ORM writes bypass service locks but still cannot defeat PostgreSQL.
    third = Meeting.objects.create(
        integration_client=client_account,
        external_class_id="CLS-3",
        class_date=start.date(),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Meeting.objects.filter(pk=third.pk).update(
            room=room,
            start_at=start,
            end_at=end,
            reservation=reserved.reservation,
            reservation_active=True,
        )


@pytest.mark.django_db
def test_inactive_and_auto(meeting, room, times):
    room.is_active = False
    room.save()
    with pytest.raises(GatewayError):
        RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    with pytest.raises(GatewayError):
        RoomAllocator.allocate_any_room(meeting, *times)
    room.is_active = True
    room.save()
    assert RoomAllocator.allocate_any_room(meeting, *times).room_id == room.pk


@pytest.mark.django_db
def test_availability_private(meeting, room, times):
    RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    data = availability(times[0].date(), *times)
    assert data[0]["available"] is False
    assert data[0]["bookedSlots"][0]["status"] == "BOOKED"
    serialized = json.dumps(data)
    for secret in (
        room.username,
        str(room.pk),
        str(meeting.pk),
        meeting.external_class_id,
        room.encrypted_password,
    ):
        assert secret not in serialized


@pytest.mark.parametrize(
    "data",
    [
        "opaque",
        {"accessToken": "opaque", "refreshToken": "private"},
        json.dumps({"accessToken": "opaque"}),
    ],
)
def test_auth_shapes(data):
    assert normalize_auth({"success": True, "data": data}).access_token == "opaque"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"success": True, "data": {}},
        {"success": True, "data": ""},
        {"success": False, "data": "bad"},
    ],
)
def test_bad_auth(body):
    with pytest.raises(ProviderError):
        normalize_auth(body)


def test_jwt_expiry():
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": 1800000000}).encode())
        .decode()
        .rstrip("=")
    )
    assert (
        normalize_auth(
            {"success": True, "data": "x." + payload + ".x"}
        ).expires_at.timestamp()
        == 1800000000
    )


@pytest.mark.parametrize(
    "config",
    [
        {"evil": True},
        {"meetingType": "bad"},
        {"config": {"MIC_OFF": "true"}},
        {"config": {"ALLOW_COUNTRY": ["bangladesh"]}},
    ],
)
def test_config_rejects(config):
    with pytest.raises(ValidationError):
        validate_config(config)


@pytest.mark.django_db
def test_config_hierarchy(client_account, room):
    MeetingConfigPreset.objects.create(
        name="System",
        is_system_default=True,
        payload={"meetingType": "scheduled", "config": {"MIC_OFF": True}},
    )
    client_account.default_preset = MeetingConfigPreset.objects.create(
        name="Client", payload={"meetingType": "instant"}
    )
    room.default_meeting_config = {"config": {"CAMERA_OFF": True}}
    assert resolve_config(client_account, room) == {
        "meetingType": "instant",
        "config": {"MIC_OFF": True, "CAMERA_OFF": True},
    }


@pytest.mark.django_db
def test_provider_adapter(room):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer fake-token"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "calendarId": "cal-1",
                    "meetingPanelAddress": "meet.convay.com",
                    "startMeetingUrl": "https://meet.convay.com/start?jwt=fake",
                },
            },
        )

    client = ConvayClient(transport=httpx.MockTransport(handler))
    try:
        value = client.start_meeting(room, {"meetingType": "instant"}, "fake-token")
        assert value.calendar_id == "cal-1"
        assert value.meeting_panel_address == "meet.convay.com"
        assert value.start_meeting_url == "https://meet.convay.com/start?jwt=fake"
        assert value.unique_id is None
    finally:
        client.close()


@pytest.mark.django_db
def test_provider_contract_title_and_success_response(meeting, room, times):
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "calendarId": "ac12000f-a08f-1cc7-81a0-cebf67ea293e",
                "meetingPanelAddress": "meet.convay.com",
                "startMeetingUrl": "https://meet.convay.com/example?jwt=fake",
            },
        )

    provider = ConvayClient(
        transport=httpx.MockTransport(handler), meeting_id=meeting.pk
    )
    with (
        patch("apps.meetings.services.ConvayClient", return_value=provider),
        patch(
            "apps.meetings.services.get_token",
            return_value=ProviderAuthResult("fake-access"),
        ),
    ):
        result, _ = provision(
            meeting,
            {
                "roomId": room.public_id,
                "startAt": times[0],
                "endAt": times[1],
            },
        )

    final_payload = captured["payload"]
    assert final_payload["title"] == meeting.meeting_title
    assert "meetingTitle" not in final_payload
    assert final_payload["meetingType"] == "instant"
    assert result.status == "READY"
    assert result.provider_calendar_id == "ac12000f-a08f-1cc7-81a0-cebf67ea293e"
    assert result.provider_panel_address == "meet.convay.com"
    assert decrypt(result.encrypted_provider_start_url) == (
        "https://meet.convay.com/example?jwt=fake"
    )


@pytest.mark.parametrize(
    "status,body,ambiguous",
    [(401, {}, False), (500, {}, True), (200, {"success": True, "data": {}}, True)],
)
@pytest.mark.django_db
def test_provider_errors(room, status, body, ambiguous):
    client = ConvayClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    )
    try:
        with pytest.raises(ProviderError) as error:
            client.start_meeting(room, {}, "fake")
        assert error.value.ambiguous == ambiguous
    finally:
        client.close()


@pytest.mark.django_db
def test_timeout_retains_reservation(meeting, room, times):
    with (
        patch(
            "apps.meetings.services.get_token", return_value=ProviderAuthResult("fake")
        ),
        patch(
            "apps.meetings.services.ConvayClient.start_meeting",
            side_effect=ProviderError("PROVIDER_TRANSPORT_ERROR", True),
        ),
    ):
        with pytest.raises(GatewayError):
            provision(
                meeting,
                {"roomId": room.public_id, "startAt": times[0], "endAt": times[1]},
            )
    meeting.refresh_from_db()
    assert meeting.status == "PROVIDER_STATE_UNKNOWN"
    assert meeting.reservation_active
    with pytest.raises(GatewayError):
        cancel(meeting)


@pytest.mark.django_db
def test_admin_masking(room):
    model_admin = RoomAdmin(Room, AdminSite())
    assert model_admin.credential_configured(room) == "Configured"
    form = RoomForm(instance=room)
    assert "fake-provider-password" not in form.as_p()
    assert room.encrypted_password not in form.as_p()
    request = RequestFactory().get("/")
    request.user = User(is_staff=True)
    assert "username" not in model_admin.get_fields(request, room)
    assert "password" not in model_admin.get_fields(request, room)


def test_redaction():
    raw = {
        "Authorization": "fake",
        "nested": {
            "accessToken": "fake",
            "client_secret": "fake",
            "startMeetingUrl": "fake",
        },
    }
    assert "fake" not in json.dumps(redact(raw))


@pytest.mark.django_db
def test_health(api):
    assert api.get("/health/live/").status_code == 200
    assert api.get("/health/ready/").status_code == 200
