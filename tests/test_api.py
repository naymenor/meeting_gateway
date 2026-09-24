import json
from unittest.mock import patch
import pytest
from rest_framework.test import APIClient
from apps.integrations.models import IntegrationClient
from apps.meetings.models import Meeting
from apps.convay.client import ProviderAuthResult, ProviderMeetingResult
from apps.audit.models import AuditLog

PAYLOAD = {
    "meetingTitle": "Physics",
    "teacher": {"id": "T-1", "name": "Teacher"},
    "batch": {"id": "B-1", "name": "Batch"},
    "class": {"id": "CLS-1", "date": "2026-09-25"},
}


@pytest.mark.django_db
def test_registration_naturally_reuses_class(api, client_account):
    first = api.post("/api/v1/meetings/", PAYLOAD, format="json")
    assert first.status_code == 201, first.data
    replay = api.post(
        "/api/v1/meetings/",
        dict(reversed(list(PAYLOAD.items()))),
        format="json",
    )
    assert replay.status_code == 200
    assert replay.data["data"]["id"] == first.data["data"]["id"]
    assert Meeting.objects.count() == 1
    changed = api.post(
        "/api/v1/meetings/",
        {**PAYLOAD, "meetingTitle": "Changed"},
        format="json",
    )
    assert changed.status_code == 200
    assert changed.data["data"]["id"] == first.data["data"]["id"]
    assert changed.data["data"]["meetingInfo"]["meetingTitle"] == "Physics"
    search = api.get("/api/v1/meetings/?teacher_name=teach&class_date=2026-09-25")
    assert search.data["data"]["count"] == 1
    assert "authorization" not in json.dumps(search.data)
    assert api.get("/api/v1/meetings/?class_date=invalid").status_code == 400
    assert api.get("/api/v1/meetings/?page_size=1000").status_code == 400


@pytest.mark.django_db
def test_different_clients_can_reuse_external_class_id(client_account):
    from apps.meetings.services import register

    first, first_created = register(client_account, PAYLOAD)
    other = IntegrationClient.objects.create(name="Other LMS")
    second, second_created = register(other, PAYLOAD)
    assert first_created and second_created
    assert first.pk != second.pk
    assert first.external_class_id == second.external_class_id == "CLS-1"


@pytest.mark.django_db
def test_foreign_meeting(api, meeting):
    other = IntegrationClient.objects.create(name="Other")
    meeting.integration_client = other
    meeting.save()
    for suffix in ["", "convay-token/", "create/", "cancel/"]:
        url = f"/api/v1/meetings/{meeting.pk}/" + suffix
        response = api.post(url, {}, format="json") if suffix else api.get(url)
        assert response.status_code == 404
    assert api.get("/api/v1/meetings/").data["data"]["count"] == 0


@pytest.mark.django_db
def test_auth_and_scope(client_account):
    api = APIClient()
    assert api.get("/api/v1/meetings/").status_code == 401
    secret = client_account.rotate_secret()
    assert secret not in client_account.secret_hash
    response = api.post(
        "/api/v1/auth/token/",
        {"client_id": str(client_account.client_id), "client_secret": secret},
        format="json",
    )
    api.credentials(HTTP_AUTHORIZATION="Bearer " + response.data["data"]["accessToken"])
    assert api.get("/api/v1/meetings/").status_code == 200
    client_account.scopes = []
    client_account.save()
    assert api.get("/api/v1/meetings/").status_code == 403
    client_account.is_active = False
    client_account.save()
    assert api.get("/api/v1/meetings/").status_code == 401


@pytest.mark.django_db
def test_creation_token_and_natural_replay(api, meeting, room, times):
    payload = {
        "roomId": room.public_id,
        "startAt": times[0].isoformat(),
        "endAt": times[1].isoformat(),
    }
    provider_data = ProviderMeetingResult(
        calendar_id="cal-fake",
        meeting_panel_address="provider.invalid",
        start_meeting_url="https://provider.invalid/start?token=fake-sensitive",
    )
    with (
        patch(
            "apps.meetings.services.get_token",
            return_value=ProviderAuthResult("fake-access"),
        ),
        patch(
            "apps.meetings.views.get_token",
            return_value=ProviderAuthResult("fake-access"),
        ),
        patch(
            "apps.meetings.services.ConvayClient.start_meeting",
            return_value=provider_data,
        ) as upstream,
    ):
        first = api.post(
            f"/api/v1/meetings/{meeting.pk}/create/",
            payload,
            format="json",
        )
        assert first.status_code == 201, first.data
        replay = api.post(
            f"/api/v1/meetings/{meeting.pk}/create/",
            payload,
            format="json",
        )
        assert replay.status_code == 200
        assert replay.data["data"]["id"] == first.data["data"]["id"]
        assert upstream.call_count == 1
        final_payload = upstream.call_args.args[1]
        assert final_payload["title"] == meeting.meeting_title
        assert "meetingTitle" not in final_payload
    meeting.refresh_from_db()
    assert meeting.status == "READY"
    assert meeting.provider_unique_id is None
    assert "fake-sensitive" not in meeting.encrypted_provider_start_url
    with patch(
        "apps.meetings.views.get_token", return_value=ProviderAuthResult("fresh-fake")
    ):
        token = api.post(f"/api/v1/meetings/{meeting.pk}/convay-token/")
        assert (
            token.data["data"]["convay"]["authorization"]["accessToken"] == "fresh-fake"
        )
    assert "fake-access" not in json.dumps(list(AuditLog.objects.values("metadata")))
    detail = api.get(f"/api/v1/meetings/{meeting.pk}/")
    assert "startMeetingUrl" not in json.dumps(detail.data)
    assert room.username not in json.dumps(first.data)


@pytest.mark.django_db
def test_creation_requires_write_scope(api, client_account, meeting):
    client_account.scopes.remove("meeting:write")
    response = api.post(f"/api/v1/meetings/{meeting.pk}/create/", {}, format="json")
    assert response.status_code == 403


@pytest.mark.django_db
def test_naive_rejected(api, meeting):
    response = api.post(
        f"/api/v1/meetings/{meeting.pk}/create/",
        {"startAt": "2026-09-25T11:00:00", "endAt": "2026-09-25T12:00:00"},
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_cancel_is_repeatable_and_cancelled_meeting_cannot_be_created(
    api, meeting, room, times
):
    url = f"/api/v1/meetings/{meeting.pk}/cancel/"
    first = api.post(url, {}, format="json")
    second = api.post(url, {}, format="json")
    assert first.status_code == second.status_code == 200
    assert first.data["data"]["meetingInfo"]["status"] == "CANCELLED"
    assert second.data["data"]["meetingInfo"]["status"] == "CANCELLED"
    assert (
        AuditLog.objects.filter(
            action="meeting_cancelled", object_id=str(meeting.pk)
        ).count()
        == 1
    )
    with patch("apps.meetings.services.ConvayClient.start_meeting") as upstream:
        create = api.post(
            f"/api/v1/meetings/{meeting.pk}/create/",
            {
                "roomId": room.public_id,
                "startAt": times[0].isoformat(),
                "endAt": times[1].isoformat(),
            },
            format="json",
        )
    assert create.status_code == 409
    assert create.data["code"] == "INVALID_STATE"
    upstream.assert_not_called()


@pytest.mark.django_db
def test_token_cache_and_rotation(room):
    from apps.convay.tokens import get_token

    with patch(
        "apps.convay.tokens.ConvayClient.authenticate",
        return_value=ProviderAuthResult("fake-cached"),
    ) as auth:
        assert get_token(room).access_token == "fake-cached"
        assert get_token(room).access_token == "fake-cached"
        assert auth.call_count == 1
        room.set_password("replaced-fake")
        room.save()
        get_token(room)
        assert auth.call_count == 2
