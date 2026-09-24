from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from datetime import date, datetime, timedelta, timezone
import pytest
from django.db import close_old_connections, connection, connections
from apps.integrations.models import IntegrationClient
from apps.rooms.models import Room
from apps.rooms.services import RoomAllocator
from apps.meetings.models import Meeting
from apps.meetings.services import provision, register
from apps.convay.client import ProviderAuthResult, ProviderMeetingResult
from common.exceptions import GatewayError
from unittest.mock import patch


@pytest.mark.django_db(transaction=True)
def test_fifty_requests_five_rooms():
    assert connection.vendor == "postgresql"
    client = IntegrationClient.objects.create(name="Concurrent LMS")
    for number in range(5):
        room = Room(
            public_id=f"ROOM-{number}",
            name=f"Room {number}",
            username=f"fake-{number}@example.invalid",
            priority=number,
        )
        room.set_password("fake")
        room.save()
    ids = [
        Meeting.objects.create(
            integration_client=client,
            external_class_id=f"CLS-{number}",
            class_date=date(2026, 9, 25),
        ).pk
        for number in range(50)
    ]
    barrier = Barrier(50)
    start = datetime(2026, 9, 25, 11, tzinfo=timezone.utc)

    def attempt(pk):
        close_old_connections()
        try:
            meeting = Meeting.objects.get(pk=pk)
            barrier.wait(timeout=30)
            try:
                result = RoomAllocator.allocate_any_room(
                    meeting, start, start + timedelta(hours=1)
                )
                return str(result.room_id)
            except GatewayError as exc:
                assert exc.default_code == "NO_ROOM_CAPACITY"
                return None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(attempt, ids))
    successes = [r for r in results if r]
    assert len(successes) == len(set(successes)) == 5
    assert results.count(None) == 45
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM meetings_meeting a JOIN meetings_meeting b ON a.id < b.id AND a.room_id = b.room_id AND a.reservation && b.reservation WHERE a.reservation_active AND b.reservation_active"
        )
        assert cursor.fetchone()[0] == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_registration_creates_one_meeting():
    client = IntegrationClient.objects.create(name="Registration LMS")
    values = {
        "meetingTitle": "Physics",
        "teacher": {"id": "T-1", "name": "Teacher"},
        "subject": {"id": "PHY", "name": "Physics"},
        "batch": {"id": "B-1", "name": "Batch"},
        "class": {"id": "CLS-CONCURRENT", "date": date(2026, 9, 25)},
        "scheduleType": "SCHEDULED",
    }
    barrier = Barrier(10)

    def attempt(_):
        close_old_connections()
        try:
            account = IntegrationClient.objects.get(pk=client.pk)
            barrier.wait(timeout=30)
            meeting, created = register(account, values)
            return meeting.pk, created
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))
    assert len({meeting_id for meeting_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert (
        Meeting.objects.filter(
            integration_client=client, external_class_id="CLS-CONCURRENT"
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_create_calls_provider_once(client_account, room, times):
    meeting = Meeting.objects.create(
        integration_client=client_account,
        external_class_id="CLS-CREATE-CONCURRENT",
        meeting_title="Physics",
        teacher_id="T-1",
        teacher_name="Teacher",
        batch_id="B-1",
        batch_name="Batch",
        class_date=times[0].date(),
    )
    entered_provider = Event()
    release_provider = Event()
    provider_calls = []
    values = {"roomId": room.public_id, "startAt": times[0], "endAt": times[1]}

    def start_meeting(*args):
        provider_calls.append(1)
        entered_provider.set()
        assert release_provider.wait(timeout=30)
        return ProviderMeetingResult(
            calendar_id="calendar-concurrent",
            meeting_panel_address="meet.convay.com",
            start_meeting_url="https://meet.convay.com/start?jwt=fake",
        )

    def attempt():
        close_old_connections()
        try:
            current = Meeting.objects.get(pk=meeting.pk)
            try:
                result, _ = provision(current, values)
                return result.status
            except GatewayError as exc:
                return exc.default_code
        finally:
            connections.close_all()

    with (
        patch(
            "apps.meetings.services.get_token",
            return_value=ProviderAuthResult("fake-access"),
        ),
        patch(
            "apps.meetings.services.ConvayClient.start_meeting",
            side_effect=start_meeting,
        ),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(attempt)
        assert entered_provider.wait(timeout=30)
        second = pool.submit(attempt)
        second_result = second.result(timeout=30)
        release_provider.set()
        first_result = first.result(timeout=30)
    assert sorted([first_result, second_result]) == ["CREATION_IN_PROGRESS", "READY"]
    assert len(provider_calls) == 1
