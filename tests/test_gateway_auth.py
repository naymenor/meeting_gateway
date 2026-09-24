import json
import uuid

import jwt
import pytest
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import IntegrationClient
from apps.integrations.tokens import issue_token

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def signing_settings(settings):
    settings.GATEWAY_JWT_SIGNING_KEY = (
        "test-only-signing-key-not-for-production-" + "x" * 32
    )


@pytest.fixture
def machine(client_account):
    api = APIClient(REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65536:x}")
    secret = client_account.rotate_secret()
    return api, secret


def exchange(api, client, secret):
    return api.post(
        "/api/v1/auth/token/",
        {"client_id": str(client.client_id), "client_secret": secret},
        format="json",
    )


def authorize(api, token):
    api.credentials(HTTP_AUTHORIZATION="Bearer " + token)


def test_valid_credentials_and_bearer(machine, client_account, settings):
    api, secret = machine
    settings.GATEWAY_ACCESS_TOKEN_TTL_SECONDS = 1234
    response = exchange(api, client_account, secret)
    assert response.status_code == 200, response.data
    data = response.data["data"]
    assert data["tokenType"] == "Bearer" and data["expiresIn"] == 1234
    assert data["scopes"] == sorted(client_account.scopes)
    token = data["accessToken"]
    claims = jwt.decode(
        token,
        settings.GATEWAY_JWT_SIGNING_KEY,
        algorithms=["HS256"],
        audience=settings.GATEWAY_JWT_AUDIENCE,
        issuer=settings.GATEWAY_JWT_ISSUER,
    )
    assert claims["sub"] == str(client_account.pk)
    assert claims["client_id"] == str(client_account.client_id)
    assert claims["exp"] - claims["iat"] == 1234
    assert claims["scopes"] == data["scopes"]
    assert uuid.UUID(claims["jti"])
    assert secret not in json.dumps(claims)
    assert "client_secret" not in claims
    assert secret not in client_account.secret_hash
    assert response["Cache-Control"] == "no-store"
    IntegrationClient.objects.filter(pk=client_account.pk).update(last_used_at=None)
    authorize(api, token)
    assert api.get("/api/v1/meetings/").status_code == 200
    client_account.refresh_from_db()
    assert client_account.last_used_at is not None


@pytest.mark.parametrize(
    "case", ["wrong_secret", "unknown", "inactive", "malformed_id", "missing_secret"]
)
def test_invalid_clients(machine, client_account, case):
    api, secret = machine
    payload = {"client_id": str(client_account.client_id), "client_secret": secret}
    if case == "wrong_secret":
        payload["client_secret"] = "wrong-secret"
    elif case == "unknown":
        payload["client_id"] = str(uuid.uuid4())
    elif case == "inactive":
        client_account.is_active = False
        client_account.save()
    elif case == "malformed_id":
        payload["client_id"] = "invalid"
    else:
        del payload["client_secret"]
    response = api.post("/api/v1/auth/token/", payload, format="json")
    assert response.status_code == 401
    assert response.data["code"] == "INVALID_CLIENT"
    assert secret not in json.dumps(response.data)


@pytest.mark.parametrize(
    "header", [None, "Bearer garbage", "Basic Zm9vOmJhcg==", "Bearer", "Bearer a b"]
)
def test_invalid_or_missing_bearer(machine, header):
    api, _ = machine
    if header:
        api.credentials(HTTP_AUTHORIZATION=header)
    response = api.get("/api/v1/meetings/")
    assert response.status_code == 401
    assert response.data["code"] == "INVALID_TOKEN"
    assert response["WWW-Authenticate"].startswith("Bearer")


def test_token_expired(machine, client_account):
    api, _ = machine
    claims = jwt.decode(
        issue_token(client_account), options={"verify_signature": False}
    )
    claims["iat"] = int(timezone.now().timestamp()) - 100
    claims["exp"] = claims["iat"] + 1
    authorize(
        api, jwt.encode(claims, settings.GATEWAY_JWT_SIGNING_KEY, algorithm="HS256")
    )
    response = api.get("/api/v1/meetings/")
    assert response.status_code == 401
    assert response.data["code"] == "TOKEN_EXPIRED"


@pytest.mark.parametrize(
    "case",
    [
        "signature",
        "algorithm",
        "audience",
        "issuer",
        "missing_jti",
        "missing_exp",
        "bad_scopes",
    ],
)
def test_token_verification(machine, client_account, case):
    api, _ = machine
    claims = jwt.decode(
        issue_token(client_account), options={"verify_signature": False}
    )
    key, algorithm = settings.GATEWAY_JWT_SIGNING_KEY, "HS256"
    if case == "signature":
        key = "a-different-signing-key-with-sufficient-length"
    elif case == "algorithm":
        algorithm = "HS384"
    elif case == "audience":
        claims["aud"] = "convay"
    elif case == "issuer":
        claims["iss"] = "another-service"
    elif case.startswith("missing_"):
        del claims[case.removeprefix("missing_")]
    else:
        claims["scopes"] = [True]
    authorize(api, jwt.encode(claims, key, algorithm=algorithm))
    response = api.get("/api/v1/meetings/")
    assert response.status_code == 401
    assert response.data["code"] == "INVALID_TOKEN"


def test_live_scope_changes(machine, client_account):
    api, _ = machine
    client_account.scopes = ["meeting:read"]
    client_account.save()
    authorize(api, issue_token(client_account))
    assert api.get("/api/v1/meetings/").status_code == 200
    response = api.get("/api/v1/rooms/availability/?date=2026-09-25")
    assert response.status_code == 403 and response.data["code"] == "INSUFFICIENT_SCOPE"
    client_account.scopes = ["room:read"]
    client_account.save()
    assert api.get("/api/v1/meetings/").status_code == 403
    assert api.get("/api/v1/rooms/availability/?date=2026-09-25").status_code == 403


@pytest.mark.parametrize("change", ["rotate", "deactivate"])
def test_token_revoked(machine, client_account, change):
    api, old_secret = machine
    authorize(api, issue_token(client_account))
    if change == "rotate":
        client_account.rotate_secret()
        assert exchange(api, client_account, old_secret).status_code == 401
    else:
        client_account.is_active = False
        client_account.save()
    response = api.get("/api/v1/meetings/")
    assert response.status_code == 401 and response.data["code"] == "INVALID_TOKEN"


def test_ip_allowlist(machine, client_account):
    api, secret = machine
    api.defaults["REMOTE_ADDR"] = "192.0.2.123"
    client_account.ip_allowlist = ["192.0.2.0/24"]
    client_account.save()
    response = exchange(api, client_account, secret)
    assert response.status_code == 200
    authorize(api, response.data["data"]["accessToken"])
    assert api.get("/api/v1/meetings/").status_code == 200
    api.defaults["REMOTE_ADDR"] = "198.51.100.123"
    assert api.get("/api/v1/meetings/").status_code == 401
    assert exchange(api, client_account, secret).data["code"] == "INVALID_CLIENT"


def test_bearer_cross_client_isolation(machine, client_account, meeting):
    api, _ = machine
    other = IntegrationClient.objects.create(name="Other", scopes=client_account.scopes)
    authorize(api, issue_token(other))
    assert api.get("/api/v1/meetings/").data["data"]["count"] == 0
    assert api.get(f"/api/v1/meetings/{meeting.pk}/").status_code == 404
    for action in ["create", "convay-token", "cancel"]:
        assert (
            api.post(
                f"/api/v1/meetings/{meeting.pk}/{action}/", {}, format="json"
            ).status_code
            == 404
        )


def test_secrets_absent_from_logs(machine, client_account, caplog):
    api, secret = machine
    response = exchange(api, client_account, secret)
    token = response.data["data"]["accessToken"]
    authorize(api, token)
    api.get("/api/v1/meetings/")
    exchange(api, client_account, "wrong-sensitive-secret")
    assert secret not in caplog.text
    assert token not in caplog.text
    assert "wrong-sensitive-secret" not in caplog.text
    assert "Bearer " not in caplog.text


def test_admin_session_cannot_authenticate_lms(machine, django_user_model):
    api, _ = machine
    admin = django_user_model.objects.create_superuser(
        username="human", password="fake-admin-password"
    )
    api.force_login(admin)
    assert api.get("/api/v1/meetings/").data["code"] == "INVALID_TOKEN"


def test_write_only_can_register(machine, client_account):
    from tests.test_api import PAYLOAD

    api, _ = machine
    client_account.scopes = ["meeting:write"]
    client_account.save()
    authorize(api, issue_token(client_account))
    response = api.post(
        "/api/v1/meetings/",
        PAYLOAD,
        format="json",
    )
    assert response.status_code == 201, response.data
