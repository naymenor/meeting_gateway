from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import pytest
from django import forms
from django.db import transaction, IntegrityError
from django.contrib.auth.models import User, Group
from django.core.management import call_command
from django.test import RequestFactory
from django.contrib.admin.sites import AdminSite
from apps.rooms.admin import RoomAdmin
from apps.rooms.models import Room
from apps.rooms.services import RoomAllocator
from apps.meetings.models import Meeting
from apps.meetings.admin import MeetingAdmin
from apps.meetings.services import provision
from apps.meetings.tasks import flag_stale_provisioning
from apps.convay.client import ConvayClient, ProviderError, ProviderAuthResult
from apps.convay.tokens import get_token
from common.exceptions import GatewayError
from common.encryption import decrypt
from apps.audit.models import AuditLog
from django.utils import timezone
from psycopg.types.range import Range


@pytest.mark.django_db
def test_buffers_enforced(meeting, room, times, client_account, settings):
    settings.ROOM_BUFFER_AFTER_MINUTES = 10
    RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    next_meeting = Meeting.objects.create(
        integration_client=client_account,
        external_class_id="CLS-BUFFER",
        class_date=times[0].date(),
    )
    with pytest.raises(GatewayError):
        RoomAllocator.reserve_specific_room(
            next_meeting, room.public_id, times[1], times[1] + timedelta(hours=1)
        )


@pytest.mark.django_db
def test_empty_range_cannot_bypass_constraint(meeting, room, times):
    with pytest.raises(IntegrityError), transaction.atomic():
        Meeting.objects.filter(pk=meeting.pk).update(
            room=room,
            start_at=times[0],
            end_at=times[1],
            reservation=Range(empty=True),
            reservation_active=True,
        )


@pytest.mark.django_db
def test_room_identity_frozen(meeting, room, times):
    RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    with pytest.raises(IntegrityError), transaction.atomic():
        Room.objects.filter(pk=room.pk).update(username="different@example.invalid")


@pytest.mark.django_db
def test_scheduled_provider_rolls_back(meeting, room, times):
    room.default_meeting_config = {"meetingType": "scheduled"}
    room.save()
    with patch("apps.meetings.services.ConvayClient.start_meeting") as upstream:
        with pytest.raises(GatewayError) as error:
            provision(
                meeting,
                {"roomId": room.public_id, "startAt": times[0], "endAt": times[1]},
            )
        assert error.value.default_code == "PROVIDER_CONTRACT_UNCONFIRMED"
        upstream.assert_not_called()
    meeting.refresh_from_db()
    assert meeting.status == "DRAFT" and not meeting.reservation_active


@pytest.mark.django_db
def test_provider_limit_separate_from_schedule(meeting, room, times, client_account):
    room.provider_active_meeting_limit = 1
    room.save()
    RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    another = Meeting.objects.create(
        integration_client=client_account,
        external_class_id="CLS-CAPACITY",
        class_date=times[0].date(),
    )
    with pytest.raises(GatewayError) as error:
        RoomAllocator.reserve_specific_room(
            another, room.public_id, times[1], times[1] + timedelta(hours=1)
        )
    assert error.value.default_code == "PROVIDER_CAPACITY"


@pytest.mark.django_db
def test_auth_failure_releases_reservation(meeting, room, times):
    with (
        patch(
            "apps.meetings.services.get_token",
            side_effect=ProviderError("PROVIDER_UNAUTHORIZED"),
        ),
        patch("apps.meetings.services.ConvayClient.start_meeting") as create,
    ):
        with pytest.raises(GatewayError):
            provision(
                meeting,
                {"roomId": room.public_id, "startAt": times[0], "endAt": times[1]},
            )
        create.assert_not_called()
    meeting.refresh_from_db()
    assert meeting.status == "FAILED"
    assert not meeting.reservation_active


@pytest.mark.django_db
def test_http_timeout_not_retried(room):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(
            "fake private provider body must not escape", request=request
        )

    client = ConvayClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as error:
            client.start_meeting(room, {}, "fake")
        assert error.value.ambiguous
        assert "private" not in str(error.value)
        assert len(calls) == 1
    finally:
        client.close()


@pytest.mark.django_db
def test_expired_token_not_issued(room):
    with patch(
        "apps.convay.tokens.ConvayClient.authenticate",
        return_value=ProviderAuthResult(
            "expired", expires_at=timezone.now() - timedelta(seconds=1)
        ),
    ):
        with pytest.raises(ProviderError, match="PROVIDER_TOKEN_EXPIRED"):
            get_token(room)


@pytest.mark.django_db
def test_role_permissions_and_forms(room):
    call_command("bootstrap_roles")
    admin = RoomAdmin(Room, AdminSite())
    for name, can_change in [("Read Only", False), ("Operations Admin", True)]:
        user = User.objects.create_user(username=name, is_staff=True)
        user.groups.add(Group.objects.get(name=name))
        request = RequestFactory().get("/")
        request.user = user
        assert admin.has_view_permission(request, room)
        assert admin.has_change_permission(request, room) is can_change
        form = admin.get_form(request, room)(instance=room)
        assert "username" not in form.fields
        assert "password" not in form.fields
        assert "encrypted_password" not in form.fields
        assert not user.has_perm("integrations.view_integrationclient")


@pytest.mark.django_db
def test_stale_provisioning_is_idempotent(meeting, room, times):
    RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    Meeting.objects.filter(pk=meeting.pk).update(
        status="PROVISIONING", updated_at=timezone.now() - timedelta(minutes=11)
    )
    flag_stale_provisioning()
    flag_stale_provisioning()
    meeting.refresh_from_db()
    assert meeting.status == "PROVIDER_STATE_UNKNOWN"
    assert meeting.reservation_active


@pytest.mark.django_db
def test_admin_can_recover_unknown_provider_result(meeting, room, times):
    RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    Meeting.objects.filter(pk=meeting.pk).update(status="PROVIDER_STATE_UNKNOWN")
    meeting.refresh_from_db()
    meeting.provider_calendar_id = "recovered-calendar"
    meeting.provider_panel_address = "meet.convay.com"
    admin_user = User.objects.create_superuser(
        username="recovery-admin", password="fake-admin-password"
    )
    request = RequestFactory().post("/")
    request.user = admin_user
    form = SimpleNamespace(
        cleaned_data={
            "recovery_start_meeting_url": "https://meet.convay.com/start?jwt=fake"
        }
    )
    MeetingAdmin(Meeting, AdminSite()).save_model(
        request, meeting, form, change=True
    )
    meeting.refresh_from_db()
    assert meeting.status == "READY"
    assert meeting.reservation_active
    assert meeting.provider_calendar_id == "recovered-calendar"
    assert meeting.provider_panel_address == "meet.convay.com"
    assert (
        decrypt(meeting.encrypted_provider_start_url)
        == "https://meet.convay.com/start?jwt=fake"
    )
    assert AuditLog.objects.filter(
        action="admin_recovered_provider_meeting", object_id=str(meeting.pk)
    ).exists()


@pytest.mark.django_db
def test_admin_recovery_never_overwrites_calendar_id(meeting, room, times):
    RoomAllocator.reserve_specific_room(meeting, room.public_id, *times)
    Meeting.objects.filter(pk=meeting.pk).update(
        status="PROVIDER_STATE_UNKNOWN",
        provider_calendar_id="existing-calendar",
    )
    meeting.refresh_from_db()
    meeting.provider_calendar_id = "different-calendar"
    request = RequestFactory().post("/")
    request.user = User.objects.create_superuser(
        username="conflict-admin", password="fake-admin-password"
    )
    form = SimpleNamespace(
        cleaned_data={
            "recovery_start_meeting_url": "https://meet.convay.com/start?jwt=fake"
        }
    )
    with pytest.raises(forms.ValidationError, match="cannot overwrite"):
        MeetingAdmin(Meeting, AdminSite()).save_model(
            request, meeting, form, change=True
        )
    meeting.refresh_from_db()
    assert meeting.provider_calendar_id == "existing-calendar"
    assert meeting.status == "PROVIDER_STATE_UNKNOWN"
