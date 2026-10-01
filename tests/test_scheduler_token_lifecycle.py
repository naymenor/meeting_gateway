from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Event
from unittest.mock import patch
import json

import jwt
import pytest
from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from redis import Redis
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.convay.client import ProviderAuthResult, ProviderError, ProviderMeetingResult, normalize_auth
from apps.convay.tokens import get_token, invalidate, token_key
from apps.integrations.models import IntegrationClient
from apps.integrations.tokens import issue_token
from apps.meetings.models import Meeting
from common.encryption import encrypt
from tests.test_api import PAYLOAD

RESULT = ProviderMeetingResult('calendar', 'meet.convay.com', 'https://meet.convay.com/start?jwt=fake')


@pytest.mark.django_db
@pytest.mark.parametrize('code', ['PROVIDER_UNAVAILABLE', 'PROVIDER_RATE_LIMITED'])
def test_safe_retry_completes_same_row(api, room, code):
    with patch('apps.meetings.services.get_token', return_value=ProviderAuthResult('fake')), patch(
        'apps.meetings.services.ConvayClient.start_meeting', side_effect=[ProviderError(code), RESULT]
    ) as provider:
        assert api.post('/api/v1/meetings/', PAYLOAD, format='json').status_code == 503
        original = Meeting.objects.get()
        assert original.status == 'RETRYABLE_FAILED' and original.reservation_active
        assert original.room_id == room.pk and original.provider_calendar_id is None
        response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
        assert response.status_code == 200
        current = Meeting.objects.get()
        assert current.pk == original.pk and current.room_id == original.room_id
        assert current.reservation == original.reservation
        assert current.status == 'READY' and current.provider_calendar_id == 'calendar'
        assert current.provider_panel_address and current.encrypted_provider_start_url
        assert provider.call_count == 2
        assert provider.call_args_list[0].args[1] == provider.call_args_list[1].args[1]
        actions = list(AuditLog.objects.values_list('action', flat=True))
        for action in ('meeting_draft_created', 'slot_reserved', 'provider_provisioning_started',
                       'provider_retry_started', 'provider_retry_succeeded', 'meeting_created'):
            assert actions.count(action) == 1
        assert 'meeting_registration_reused' not in actions


@pytest.mark.django_db
@pytest.mark.parametrize('path,value', [
    (('meetingTitle',), 'Different'), (('teacher', 'id'), 'other'),
    (('teacher', 'name'), 'Other'), (('batch', 'id'), 'other'),
    (('batch', 'name'), 'Other'), (('class', 'date'), '2026-09-26'),
    (('startAt',), '2026-09-25T17:10:00+06:00'),
    (('endAt',), '2026-09-25T18:10:00+06:00'),
])
def test_retry_mismatch_preserves_original(api, room, path, value):
    with patch('apps.meetings.services.get_token', side_effect=ProviderError('PROVIDER_UNAVAILABLE')):
        api.post('/api/v1/meetings/', PAYLOAD, format='json')
    original = Meeting.objects.values().get()
    payload = deepcopy(PAYLOAD)
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    if path == ('class', 'date'):
        payload['startAt'] = '2026-09-26T17:00:00+06:00'
        payload['endAt'] = '2026-09-26T18:00:00+06:00'
    with patch('apps.meetings.services.ConvayClient.start_meeting') as provider:
        response = api.post('/api/v1/meetings/', payload, format='json')
    assert response.status_code == 409
    assert response.data['code'] == 'EXISTING_MEETING_MISMATCH'
    assert Meeting.objects.values().get() == original
    provider.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_concurrent_scheduler_retry_commits_before_provider(api, room, client_account):
    with patch('apps.meetings.services.get_token', side_effect=ProviderError('PROVIDER_UNAVAILABLE')):
        api.post('/api/v1/meetings/', PAYLOAD, format='json')
    original = Meeting.objects.get()
    entered, release = Event(), Event()

    def provider(*args):
        entered.set()
        assert release.wait(15)
        return RESULT

    def request():
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(user=IntegrationClient.objects.get(pk=client_account.pk))
            return client.post('/api/v1/meetings/', PAYLOAD, format='json')
        finally:
            connections.close_all()

    with patch('apps.meetings.services.get_token', return_value=ProviderAuthResult('fake')), patch(
        'apps.meetings.services.ConvayClient.start_meeting', side_effect=provider
    ) as upstream, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request)
        try:
            assert entered.wait(15)
            original.refresh_from_db()
            assert original.status == 'PROVISIONING' and original.reservation_active
            second = pool.submit(request).result(15)
            assert second.status_code == 409 and second.data['code'] == 'CREATION_IN_PROGRESS'
        finally:
            release.set()
        assert first.result(15).status_code == 200
    assert upstream.call_count == 1 and Meeting.objects.count() == 1


@pytest.mark.django_db
def test_gateway_default_ttl_and_expired_post_no_mutation(client_account, room):
    assert settings.GATEWAY_ACCESS_TOKEN_TTL_SECONDS == 3600
    api = APIClient()
    secret = client_account.rotate_secret()
    response = api.post('/api/v1/auth/token/', {
        'client_id': str(client_account.client_id), 'client_secret': secret,
    }, format='json')
    assert response.data['data']['expiresIn'] == 3600
    claims = jwt.decode(response.data['data']['accessToken'], options={'verify_signature': False})
    assert claims['exp'] - claims['iat'] == 3600
    claims['iat'] = int(timezone.now().timestamp()) - 3700
    claims['exp'] = claims['iat'] + 3600
    api.credentials(HTTP_AUTHORIZATION='Bearer ' + jwt.encode(
        claims, settings.GATEWAY_JWT_SIGNING_KEY, algorithm='HS256'))
    before_room = type(room).objects.values().get(pk=room.pk)
    before_client = IntegrationClient.objects.values().get(pk=client_account.pk)
    audits = AuditLog.objects.count()
    with patch('apps.meetings.services.ConvayClient.start_meeting') as provider, patch(
        'apps.meetings.services.RoomAllocator.allocate_any_room'
    ) as allocate:
        response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 401 and response.data['code'] == 'TOKEN_EXPIRED'
    assert not Meeting.objects.exists() and AuditLog.objects.count() == audits
    assert type(room).objects.values().get(pk=room.pk) == before_room
    assert IntegrationClient.objects.values().get(pk=client_account.pk) == before_client
    provider.assert_not_called()
    allocate.assert_not_called()


@pytest.mark.django_db
def test_provider_six_hour_cache_and_skew(room, settings):
    settings.CONVAY_TOKEN_EXPIRY_SKEW_SECONDS = 300
    expiry = timezone.now() + timedelta(hours=6)
    cache = Redis.from_url(settings.REDIS_URL)
    invalidate(room)
    with patch('apps.convay.tokens.ConvayClient.authenticate', return_value=ProviderAuthResult('fake', expires_at=expiry)) as auth:
        assert get_token(room).expires_at == expiry
        assert get_token(room).expires_at == expiry
        assert auth.call_count == 1
        assert 21290 <= cache.ttl(token_key(room)) <= 21300
        cache.setex(token_key(room), 600, encrypt(json.dumps({
            'token': 'near-expiry', 'expiry': (timezone.now() + timedelta(seconds=299)).isoformat(),
        })))
        assert get_token(room).access_token == 'fake'
        assert auth.call_count == 2
    invalidate(room)


def test_provider_expiry_sources():
    expiry = (timezone.now() + timedelta(hours=6)).replace(microsecond=0)
    token = jwt.encode({'exp': int(expiry.timestamp())}, 'fake-long-test-key-for-token-hint-only', algorithm='HS256')
    assert normalize_auth({'success': True, 'data': token}).expires_at == expiry
    assert normalize_auth({'success': True, 'data': {'accessToken': 'opaque', 'expiresAt': expiry.isoformat()}}).expires_at == expiry
    relative = normalize_auth({'success': True, 'data': {'accessToken': 'opaque', 'expiresIn': 21600}})
    assert 21598 < (relative.expires_at - timezone.now()).total_seconds() <= 21600


@pytest.mark.django_db
def test_returned_provider_expiry(api, room):
    expiry = timezone.now() + timedelta(hours=6)
    with patch('apps.meetings.services.get_token', return_value=ProviderAuthResult('fake', expires_at=expiry)), patch(
        'apps.meetings.services.ConvayClient.start_meeting', return_value=RESULT
    ):
        response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.data['data']['convay']['authorization']['expiresAt'] == expiry.isoformat()


def test_swagger_lifecycles_and_retry():
    schema = SchemaGenerator().get_schema(public=True)
    paths = schema['paths']
    auth = paths['/api/v1/auth/token/']['post']['description']
    creation = paths['/api/v1/meetings/']['post']['description']
    assert '60 minutes' in auth and 'provider-controlled' in auth and 'TOKEN_EXPIRED' in auth
    assert 'RETRYABLE_FAILED' in creation and 'EXISTING_MEETING_MISMATCH' in creation
    assert 'same logical Meeting' in creation and 'expiresAt' in auth


@pytest.mark.django_db
def test_retry_with_known_provider_fields_requires_review(api, room):
    with patch('apps.meetings.services.get_token', side_effect=ProviderError('PROVIDER_UNAVAILABLE')):
        api.post('/api/v1/meetings/', PAYLOAD, format='json')
    Meeting.objects.update(provider_calendar_id='already-created')
    with patch('apps.meetings.services.ConvayClient.start_meeting') as provider:
        response = api.post('/api/v1/meetings/', PAYLOAD, format='json')
    assert response.status_code == 409
    assert response.data['code'] == 'PROVIDER_RECONCILIATION_REQUIRED'
    provider.assert_not_called()
