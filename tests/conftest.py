import pytest
from cryptography.fernet import Fernet
from datetime import datetime, timedelta, timezone
from apps.integrations.models import IntegrationClient, SCOPES
from apps.rooms.models import Room
from apps.meetings.models import Meeting


@pytest.fixture(autouse=True)
def test_settings(settings):
    settings.CREDENTIAL_ENCRYPTION_KEY = Fernet.generate_key().decode()
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def client_account(db):
    return IntegrationClient.objects.create(name="LMS", scopes=sorted(SCOPES))


@pytest.fixture
def room(db):
    room = Room(public_id="ROOM-01", name="Room 1", username="fake@example.invalid")
    room.set_password("fake-provider-password")
    room.save()
    return room


@pytest.fixture
def times():
    start = datetime(2026, 9, 25, 11, tzinfo=timezone.utc)
    return start, start + timedelta(hours=1)


@pytest.fixture
def meeting(client_account, times):
    return Meeting.objects.create(
        integration_client=client_account,
        external_class_id="CLS-1",
        teacher_id="T-1",
        teacher_name="Teacher",
        batch_id="B-1",
        batch_name="Batch",
        meeting_title="Physics",
        class_date=times[0].date(),
    )


@pytest.fixture
def api(client_account):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=client_account)
    return client
