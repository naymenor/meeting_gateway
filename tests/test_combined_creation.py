from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connections
from rest_framework.test import APIClient

from apps.convay.client import ProviderAuthResult, ProviderMeetingResult
from apps.integrations.models import IntegrationClient
from apps.meetings.models import Meeting
from tests.test_api import PAYLOAD


@pytest.mark.django_db
def test_new_creation_and_snapshot_conflict(api, room, provider_success):
    availability = api.get('/api/v1/rooms/availability/', {
        'start_at': PAYLOAD['startAt'], 'end_at': PAYLOAD['endAt'],
    })
    assert availability.data['data']['available'] is True
    first = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert first.status_code == 201
    data = first.data['data']
    assert data['meetingInfo']['status'] == 'READY'
    assert 'subject' not in data['classInfo']
    stored = Meeting.objects.get(pk=data['id'])
    assert stored.start_at.isoformat() == '2026-09-25T11:00:00+00:00'
    assert stored.subject_id == stored.subject_name == ''
    second = api.post('/api/v1/meetings/', {
        **PAYLOAD, 'class': {**PAYLOAD['class'], 'id': 'CLS-2'},
    }, format='json')
    assert second.status_code == 409
    assert second.data['code'] == 'NO_CAPACITY_AVAILABLE'
    assert provider_success.call_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize('state,code', [
    ('RESERVED', 'CREATION_IN_PROGRESS'),
    ('PROVISIONING', 'CREATION_IN_PROGRESS'),
    ('PROVIDER_STATE_UNKNOWN', 'PROVIDER_RECONCILIATION_REQUIRED'),
    ('PROVIDER_RESPONSE_INVALID', 'PROVIDER_RECONCILIATION_REQUIRED'),
    ('FAILED', 'PREVIOUS_CREATION_FAILED'),
])
def test_duplicate_state_guards(api, meeting, state, code, provider_success):
    meeting.status = state
    meeting.save()
    response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 409
    assert response.data['code'] == code
    provider_success.assert_not_called()


@pytest.mark.django_db
def test_live_duplicate_and_different_client(api, room, provider_success):
    first = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    Meeting.objects.filter(pk=first.data['data']['id']).update(status='LIVE')
    replay = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert replay.status_code == 200
    assert replay.data['data']['meetingInfo']['status'] == 'LIVE'
    other = IntegrationClient.objects.create(name='Other', scopes=['meeting:write'])
    api.force_authenticate(user=other)
    response = api.post('/api/v1/meetings/', {
        **PAYLOAD, 'startAt': '2026-09-25T20:00:00+06:00', 'endAt': '2026-09-25T21:00:00+06:00',
    }, format='json')
    assert response.status_code == 201
    assert response.data['data']['id'] != first.data['data']['id']
    assert provider_success.call_count == 2


@pytest.mark.django_db
@pytest.mark.parametrize('change', [
    {'subject': {'id': 'x', 'name': 'x'}},
    {'startAt': '2026-09-25T17:00:00'},
    {'endAt': '2026-09-25T16:00:00+06:00'},
    {'roomId': ''},
])
def test_invalid_contract_has_no_provider_side_effect(api, change, provider_success):
    response = api.post('/api/v1/meetings/', {**PAYLOAD, **change}, format='json')
    assert response.status_code == 400
    assert not Meeting.objects.exists()
    provider_success.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('same_class', [True, False])
def test_concurrent_combined_posts(client_account, room, same_class):
    barrier = Barrier(2)
    entered = Event()
    release = Event()

    def provider(*args):
        entered.set()
        assert release.wait(15)
        return ProviderMeetingResult(calendar_id='fake', meeting_panel_address='meet.convay.com',
                                     start_meeting_url='https://meet.convay.com/start?jwt=fake')

    def attempt(number):
        close_old_connections()
        try:
            api = APIClient()
            api.force_authenticate(user=IntegrationClient.objects.get(pk=client_account.pk))
            payload = {**PAYLOAD, 'class': {**PAYLOAD['class'], 'id': 'same' if same_class else str(number)}}
            barrier.wait(15)
            response = api.post('/api/v1/meetings/', payload, format='json')
            if response.status_code == 409:
                release.set()
            return response.status_code, response.data
        finally:
            connections.close_all()

    with (
        patch('apps.meetings.services.get_token', return_value=ProviderAuthResult('fake')),
        patch('apps.meetings.services.ConvayClient.start_meeting', side_effect=provider) as upstream,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(attempt, i) for i in range(2)]
        try:
            assert entered.wait(15)
            results = [future.result(20) for future in futures]
        finally:
            release.set()
    # Either request may claim the committed draft. Completing a row created
    # by the other request returns 200; the creator returns 201 if it wins.
    statuses = sorted(status for status, _ in results)
    assert statuses in ([[200, 409], [201, 409]] if same_class else [[201, 409]])
    conflict = next(data for status, data in results if status == 409)
    assert conflict['code'] == ('CREATION_IN_PROGRESS' if same_class else 'NO_CAPACITY_AVAILABLE')
    assert upstream.call_count == 1
    assert Meeting.objects.filter(reservation_active=True).count() == 1
    assert Meeting.objects.count() == (1 if same_class else 2)
