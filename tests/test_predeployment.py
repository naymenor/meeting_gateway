import json
from unittest.mock import patch

import pytest
from django.urls import Resolver404, resolve

from apps.convay.client import ProviderAuthResult
from apps.convay.tokens import invalidate
from apps.meetings.models import Meeting
from common.logging import SafeJSONFormatter
from tests.test_api import PAYLOAD


@pytest.mark.django_db
@pytest.mark.parametrize('state,code', [
    ('READY', None), ('LIVE', None),
    ('RESERVED', 'CREATION_IN_PROGRESS'), ('PROVISIONING', 'CREATION_IN_PROGRESS'),
    ('PROVIDER_STATE_UNKNOWN', 'PROVIDER_RECONCILIATION_REQUIRED'),
    ('PROVIDER_RESPONSE_INVALID', 'PROVIDER_RECONCILIATION_REQUIRED'),
    ('ENDED', 'CLASS_ALREADY_COMPLETED'), ('FAILED', 'PREVIOUS_CREATION_FAILED'),
    ('CANCELLED', 'CLASS_CANCELLED'), ('OLD_UNKNOWN_STATE', 'EXISTING_MEETING_REQUIRES_REVIEW'),
])
def test_existing_states(api, meeting, room, provider_success, state, code):
    meeting.status = state
    meeting.room = room
    meeting.provider_calendar_id = 'existing-calendar'
    meeting.save()
    response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == (409 if code else 200)
    if code:
        assert response.data['code'] == code
    else:
        assert response.data['data']['id'] == str(meeting.pk)
        assert response.data['data']['convay']['calendarId'] == 'existing-calendar'
    provider_success.assert_not_called()


@pytest.mark.django_db
def test_legacy_duplicates_fail_closed(api, client_account, caplog, provider_success):
    # Modern PostgreSQL prevents duplicates. Simulate the ORM outcome on a legacy DB.
    with patch('apps.meetings.services.Meeting.objects.get_or_create',
               side_effect=Meeting.MultipleObjectsReturned('private database details')):
        response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 409
    assert response.data['code'] == 'EXISTING_MEETING_REQUIRES_REVIEW'
    assert response.data['details'] == {}
    assert 'private database details' not in json.dumps(response.data)
    events = [r for r in caplog.records if r.name == 'gateway.meetings']
    assert len(events) == 1
    event = json.loads(SafeJSONFormatter().format(events[0]))
    assert event['integration_client_id'] == str(client_account.pk)
    assert event['external_class_id'] == PAYLOAD['class']['id']
    assert event['request_id'] == response['X-Request-ID']
    provider_success.assert_not_called()
    assert not Meeting.objects.exists()


@pytest.mark.django_db
def test_overlap_boundary_and_ready_replay(api, room, settings, provider_success):
    settings.ROOM_BUFFER_BEFORE_MINUTES = settings.ROOM_BUFFER_AFTER_MINUTES = 0
    first = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert first.status_code == 201
    data = first.data['data']
    assert data['meetingInfo']['status'] == 'READY'
    assert 'roomInfo' not in data
    assert data['convay']['meetingPanelAddress']
    assert data['convay']['authorization']['accessToken']
    assert data['convay']['startMeetingUrl']
    assert 'subject' not in json.dumps(data)
    replay = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert replay.status_code == 200
    assert replay.data['data']['id'] == data['id']
    assert replay.data['data']['convay']['calendarId'] == data['convay']['calendarId']
    assert provider_success.call_count == 1
    conflict = api.post('/api/v1/meetings/', {
        **PAYLOAD, 'class': {**PAYLOAD['class'], 'id': 'overlap'},
        'startAt': '2026-09-25T17:30:00+06:00', 'endAt': '2026-09-25T18:30:00+06:00',
    }, format='json')
    assert conflict.status_code == 409
    assert conflict.data['code'] == 'NO_CAPACITY_AVAILABLE'
    assert provider_success.call_count == 1
    boundary = api.post('/api/v1/meetings/', {
        **PAYLOAD, 'class': {**PAYLOAD['class'], 'id': 'boundary'},
        'startAt': '2026-09-25T18:00:00+06:00', 'endAt': '2026-09-25T19:00:00+06:00',
    }, format='json')
    assert boundary.status_code == 201
    assert provider_success.call_count == 2


@pytest.mark.django_db
def test_token_endpoint_cache_and_reauthentication(api, meeting, room, settings):
    meeting.room = room
    meeting.status = 'READY'
    meeting.save()
    url = f'/api/v1/meetings/{meeting.pk}/convay-token/'
    invalidate(room)
    with patch('apps.convay.tokens.ConvayClient.authenticate', side_effect=[
        ProviderAuthResult('first-access'), ProviderAuthResult('new-access'),
    ]) as auth:
        first = api.post(url)
        repeat = api.post(url)
        assert first.status_code == repeat.status_code == 200
        assert first.data == repeat.data
        assert auth.call_count == 1
        invalidate(room)
        fresh = api.post(url)
        assert fresh.status_code == 200
        assert fresh.data['data']['convay']['authorization']['accessToken'] == 'new-access'
        assert auth.call_count == 2
    body = json.dumps(fresh.data)
    for secret in ('refreshToken', 'username', 'password', 'encryption', room.username,
                   settings.CREDENTIAL_ENCRYPTION_KEY):
        assert secret not in body
    invalidate(room)


@pytest.mark.django_db
def test_removed_routes_not_registered(meeting):
    for action in ('create', 'cancel'):
        with pytest.raises(Resolver404):
            resolve(f'/api/v1/meetings/{meeting.pk}/{action}/')


@pytest.mark.django_db
def test_unexpected_exception_is_generic_and_diagnostics_are_safe(api, settings, caplog):
    def fail(*args, **kwargs):
        password = 'private-password'
        access_token = 'private-access'
        refresh_token = 'private-refresh'
        client_secret = 'private-secret'
        raise RuntimeError(
            f'Broken invariant {password} {access_token} {refresh_token} {client_secret} '
            f'{settings.CREDENTIAL_ENCRYPTION_KEY} '
            'https://meet.convay.com/start?jwt=private-url '
            'eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjEyMzQ1Njc4OX0.signature'
        )
    with patch('apps.meetings.views.register', side_effect=fail):
        response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 500
    assert response.data == {
        'success': False, 'code': 'INTERNAL_ERROR',
        'message': 'An internal error occurred.', 'details': {},
        'requestId': response['X-Request-ID'],
    }
    record = next(r for r in caplog.records if r.name == 'gateway.errors')
    raw = json.dumps(record.msg)
    formatted = SafeJSONFormatter().format(record)
    for secret in ('private-', settings.CREDENTIAL_ENCRYPTION_KEY, 'eyJhbGci'):
        assert secret not in raw + formatted + json.dumps(response.data)
    event = json.loads(formatted)
    assert event['exception_class'] == 'RuntimeError'
    assert 'Broken invariant' in event['exception_message']
    assert event['method'] == 'POST'
    assert event['route'] == 'api/v1/meetings/'
    assert event['request_id'] == response['X-Request-ID']
    assert any(frame['function'] == 'fail' for frame in event['traceback'][0]['frames'])
    assert 'locals' not in formatted
