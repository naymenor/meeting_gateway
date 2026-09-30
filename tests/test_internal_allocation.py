import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connections, connection
from rest_framework.test import APIClient

from apps.convay.client import ProviderError
from apps.integrations.models import IntegrationClient
from apps.meetings.models import Meeting
from apps.rooms.models import Room, MeetingConfigPreset
from tests.test_api import PAYLOAD

QUERY = {'start_at': PAYLOAD['startAt'], 'end_at': PAYLOAD['endAt']}
URL = '/api/v1/rooms/availability/'


def make_room(number, priority):
    room = Room(public_id=f'LICENSE-{number}', name=f'Private name {number}',
                username=f'private-{number}@example.invalid', priority=priority)
    room.set_password('private-password')
    room.save()
    return room


def assert_private(data, rooms):
    body = json.dumps(data)
    for field in ('roomId', 'roomInfo', 'roomName', 'room_id', 'rooms', 'priority', 'username'):
        assert field not in body
    for room in rooms:
        for value in (room.public_id, room.name, room.username, str(room.pk)):
            assert value not in body


@pytest.mark.django_db
def test_priority_fallback_privacy_and_replay(api, provider_success):
    # Names and insertion order deliberately differ from allocation order.
    third = make_room('0', 3)
    second = make_room('B', 1)
    first = make_room('A', 1)
    expected = [first, second, third]
    ids = []
    for index, room in enumerate(expected):
        snapshot = api.get(URL, QUERY)
        assert snapshot.status_code == 200
        assert snapshot.data['data'] == {
            'startAt': '2026-09-25T11:00:00Z',
            'endAt': '2026-09-25T12:00:00Z', 'available': True,
        }
        assert_private(snapshot.data, expected)
        payload = {**PAYLOAD, 'class': {**PAYLOAD['class'], 'id': str(index)}}
        created = api.post('/api/v1/meetings/', payload, format='json')
        assert created.status_code == 201
        meeting_id = created.data['data']['id']
        ids.append(meeting_id)
        assert Meeting.objects.get(pk=meeting_id).room_id == room.pk
        assert_private(created.data, expected)
        assert_private(api.get(f'/api/v1/meetings/{meeting_id}/').data, expected)
        token = api.post(f'/api/v1/meetings/{meeting_id}/convay-token/')
        assert token.status_code == 200
        assert_private(token.data, expected)
        replay = api.post('/api/v1/meetings/', payload, format='json')
        assert replay.status_code == 200
        assert replay.data['data']['id'] == meeting_id
        assert Meeting.objects.get(pk=meeting_id).room_id == room.pk
        assert provider_success.call_count == index + 1
    assert_private(api.get('/api/v1/meetings/').data, expected)
    snapshot = api.get(URL, QUERY)
    assert snapshot.status_code == 200 and snapshot.data['data']['available'] is False
    exhausted = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert exhausted.status_code == 409
    assert exhausted.data['code'] == 'NO_CAPACITY_AVAILABLE'
    assert exhausted.data['message'] == 'No meeting capacity is available for the requested time.'
    assert exhausted.data['details'] == {}
    assert_private(exhausted.data, expected)
    assert provider_success.call_count == 3


@pytest.mark.django_db
@pytest.mark.parametrize('query', [
    {}, {'start_at': PAYLOAD['startAt']}, {'end_at': PAYLOAD['endAt']},
    {**QUERY, 'start_at': 'not-a-date'}, {**QUERY, 'end_at': 'not-a-date'},
    {**QUERY, 'start_at': '2026-09-25T11:00:00'},
    {**QUERY, 'end_at': '2026-09-25T12:00:00'},
    {**QUERY, 'end_at': QUERY['start_at']},
    {**QUERY, 'end_at': '2026-09-25T10:00:00Z'},
    {**QUERY, 'date': '2026-09-25'},
    {**QUERY, 'roomId': 'ROOM-01'},
    {**QUERY, 'start_at': '2026-09-25T17:00:00 06:00'},
    {**QUERY, 'start_at': '2026-09-25T17:00:00+99:00'},
    {**QUERY, 'start_at': ''},
])
def test_availability_validation_is_json_400(api, query):
    response = api.get(URL, query)
    assert response.status_code == 400
    assert response['Content-Type'].startswith('application/json')
    assert response.data['success'] is False
    assert response.data['requestId'] == response['X-Request-ID']


@pytest.mark.django_db
@pytest.mark.parametrize('changes', [
    {'is_active': False}, {'username': ''}, {'encrypted_password': ''},
    {'encrypted_password': 'unreadable'},
    {'default_meeting_config': {'meetingType': 'scheduled'}},
    {'default_meeting_config': {'unknown': True}},
])
def test_ineligible_room_skipped_for_snapshot_and_creation(api, room, changes, provider_success):
    for name, value in changes.items():
        setattr(room, name, value)
    room.save()
    assert api.get(URL, QUERY).data['data']['available'] is False
    response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 409
    assert response.data['code'] == 'NO_CAPACITY_AVAILABLE'
    provider_success.assert_not_called()
    fallback = make_room('fallback', 200)
    assert api.get(URL, QUERY).data['data']['available'] is True
    response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 201
    assert Meeting.objects.get(pk=response.data['data']['id']).room_id == fallback.pk


@pytest.mark.django_db
def test_provider_limit_and_buffers_affect_snapshot(api, room, provider_success, settings):
    settings.ROOM_BUFFER_AFTER_MINUTES = 15
    assert api.post('/api/v1/meetings/', PAYLOAD, format='json').status_code == 201
    adjacent = {'start_at': '2026-09-25T18:00:00+06:00', 'end_at': '2026-09-25T19:00:00+06:00'}
    assert api.get(URL, adjacent).data['data']['available'] is False
    settings.ROOM_BUFFER_AFTER_MINUTES = 0
    later = {'start_at': '2026-09-25T19:00:00+06:00', 'end_at': '2026-09-25T20:00:00+06:00'}
    assert api.get(URL, later).data['data']['available'] is True
    room.provider_active_meeting_limit = 1
    room.save()
    assert api.get(URL, later).data['data']['available'] is False
    response = api.post('/api/v1/meetings/', {
        **PAYLOAD, 'class': {**PAYLOAD['class'], 'id': 'next'},
        'startAt': later['start_at'], 'endAt': later['end_at'],
    }, format='json')
    assert response.status_code == 409 and response.data['code'] == 'NO_CAPACITY_AVAILABLE'
    assert provider_success.call_count == 1


@pytest.mark.django_db
def test_unknown_provider_outcome_retains_capacity(api, room, provider_success):
    provider_success.side_effect = ProviderError('PROVIDER_STATE_UNKNOWN', ambiguous=True)
    response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 502
    meeting = Meeting.objects.get(external_class_id=PAYLOAD['class']['id'])
    assert meeting.status == 'PROVIDER_STATE_UNKNOWN'
    assert meeting.reservation_active and meeting.room_id == room.pk
    assert api.get(URL, QUERY).data['data']['available'] is False
    replay = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert replay.status_code == 409
    assert replay.data['code'] == 'PROVIDER_RECONCILIATION_REQUIRED'
    assert provider_success.call_count == 1


@pytest.mark.django_db
def test_client_configuration_is_used_for_availability(api, client_account, room, provider_success):
    client_account.default_preset = MeetingConfigPreset.objects.create(
        name='scheduled', payload={'meetingType': 'scheduled'})
    client_account.save()
    assert api.get(URL, QUERY).data['data']['available'] is False
    response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 409
    provider_success.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_five_concurrent_posts_four_licenses(client_account, provider_success):
    rooms = [make_room(i, i) for i in range(4)]
    barrier = Barrier(5)

    def attempt(number):
        close_old_connections()
        try:
            api = APIClient()
            api.force_authenticate(user=IntegrationClient.objects.get(pk=client_account.pk))
            barrier.wait(20)
            response = api.post('/api/v1/meetings/', {
                **PAYLOAD, 'class': {**PAYLOAD['class'], 'id': f'concurrent-{number}'},
            }, format='json')
            return response.status_code, response.data
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(attempt, range(5)))
    assert sorted(status for status, _ in results) == [201, 201, 201, 201, 409]
    assert next(data for status, data in results if status == 409)['code'] == 'NO_CAPACITY_AVAILABLE'
    assert provider_success.call_count == 4
    assigned = list(Meeting.objects.filter(reservation_active=True).values_list('room_id', flat=True))
    assert len(assigned) == len(set(assigned)) == 4
    assert set(assigned) == {room.pk for room in rooms}
    with connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM meetings_meeting a JOIN meetings_meeting b ON a.id < b.id AND a.room_id = b.room_id AND a.reservation && b.reservation WHERE a.reservation_active AND b.reservation_active')
        assert cursor.fetchone()[0] == 0
