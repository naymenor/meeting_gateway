# Meeting Gateway

Django 5.2 service for trusted LMS backends: register class metadata, inspect room occupancy, reserve a room without collisions, create a Convay meeting, and retrieve the account access token by Gateway Meeting UUID. Human administration uses Django Admin. The LMS performs subsequent Convay operations directly.

This implementation uses PostgreSQL exclusively, including its integration tests. No provider credentials are bundled. Provider traffic is mocked in the automated suite; confirmed live behavior and the remaining provider ambiguities are recorded separately in [contract notes](docs/convay-contract.md).

## WSL2 / Docker setup

Run from the WSL filesystem with Docker Desktop WSL integration enabled. No host Python or PostgreSQL is required.

```bash
docker compose build web
docker compose up -d db redis
# Generate a local key without writing it to source or printing it.
export CREDENTIAL_ENCRYPTION_KEY="$(docker compose run --rm --no-deps web python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py bootstrap_roles
docker compose run --rm web python manage.py collectstatic --noinput
docker compose run --rm web python manage.py createsuperuser
docker compose up -d web worker beat
docker compose run --rm web pytest -q -p no:cacheprovider
```

Keep the encryption key in a password manager or secret store; reuse it across restarts. Generating a new key makes existing encrypted records unreadable unless the old key is supplied as a previous key. An empty key permits health checks and schema setup, but cannot save credentials or idempotent mutation results. `.env.example` is the only distributed environment file. Set `GATEWAY_ENV_FILE` to a protected local environment file if preferred; the Compose encryption-key override requires the key to be exported separately. Never commit environment secrets.

The default PostgreSQL host port is `127.0.0.1:5433`, while containers use `db:5432`. Redis has no host port. A local, ignored `docker-compose.override.yml` currently disables PostgreSQL host forwarding because this workstation's Docker Desktop port-forwarding service fails. This does not affect internal access. Remove or adjust that override after fixing Docker Desktop if host database access is needed.

Visit `/admin/`, `/api/docs/` (staff session required), `/health/live/`, and `/health/ready/` on port 8000. If host forwarding is unavailable, readiness can be tested inside the web container:

```bash
docker compose exec web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready/').read().decode())"
```

Create an Integration Client in Admin, assign its scopes, and copy its secret from the one-time response. Create rooms with fake credentials for development or enter real credentials privately when ready for provider acceptance testing. Configure a system MeetingConfigPreset and optional client/room overrides. Operations staff and read-only staff receive the groups created by `bootstrap_roles`; also set their `is_staff` flag. Only superusers manage users, API clients, presets, and credentials.

## Project tree

```text
apps/
  accounts/       human administrator role bootstrap
  integrations/  machine clients, hashed secrets, authentication
  rooms/         encrypted credentials, presets, allocator, availability
  meetings/      registration, reservation, provider orchestration, APIs
  convay/        httpx adapter and encrypted Redis token cache
  audit/         append-only admin audit view and safe event writer
common/          encryption, errors, health, logging, request IDs
config/          settings, URLs, WSGI and Celery
tests/           API, provider, security and PostgreSQL concurrency tests
docs/            architecture, API workflow, provider contract
Dockerfile
docker-compose.yml
docker-compose.production.yml
.env.example
requirements.txt
requirements.lock
```

Models include Django users/groups, IntegrationClient, Room, MeetingConfigPreset, Meeting, a legacy internal IdempotencyRecord, and AuditLog. PostgreSQL migrations enforce per-client class identity, finite half-open reservations, and collision prevention with `btree_gist`.

## API routes

| Method | Route | Scope |
|---|---|---|
| POST | `/api/v1/auth/token/` | Client ID and secret exchange |
| POST | `/api/v1/meetings/` | `meeting:write` |
| GET | `/api/v1/meetings/` | `meeting:read` |
| GET | `/api/v1/meetings/{uuid}/` | `meeting:read` |
| POST | `/api/v1/meetings/{uuid}/create/` | `meeting:write` |
| POST | `/api/v1/meetings/{uuid}/convay-token/` | `meeting:token` |
| POST | `/api/v1/meetings/{uuid}/cancel/` | `meeting:cancel` |
| GET | `/api/v1/rooms/availability/` | `room:read` |
| GET | `/health/live/`, `/health/ready/` | None |
| GET | `/api/schema/`, `/api/docs/` | Human staff session |

LMS authentication uses `POST /api/v1/auth/token/` with JSON `client_id` and `client_secret`. Use the returned Gateway JWT in `Authorization: Bearer <accessToken>` on LMS API requests. Basic authentication and Django session login are not accepted by LMS endpoints. Token lifetime defaults to `GATEWAY_ACCESS_TOKEN_TTL_SECONDS=3600`. Swagger's **Authorize** button accepts this Gateway token; it is distinct from the delegated Convay token. The documentation UI remains restricted to human staff sessions.

Secrets remain hashed and are shown once through Super Admin creation/rotation. Secret rotation invalidates existing Gateway tokens; deactivation and IP-allowlist changes are enforced immediately. Tokens use the intersection of their issued scopes and the client's current scopes. Added scopes require a new token. Signing uses `GATEWAY_JWT_SIGNING_KEY` when supplied, otherwise `DJANGO_SECRET_KEY`; use a strong private key value consistently across web instances. Rotating that signing key invalidates all Gateway tokens. No refresh-token or parallel Basic scheme is implemented.

Meeting registration and creation require `meeting:write`. Creation includes Convay authorization and its sensitive start URL only if the caller also has `meeting:token`. The public API requires no idempotency header: registration reuses the meeting identified by client plus `class.id`, creation returns an existing READY result, and cancellation is repeatable.

See [workflow](docs/api-workflow.md) for fake examples and error behavior.

## Deployment

Use the production overlay with a protected `GATEWAY_ENV_FILE`, exported `CREDENTIAL_ENCRYPTION_KEY` and `POSTGRES_PASSWORD`, a strong Django secret, explicit hosts/origins, private networking, durable volume backups, and TLS termination:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml build web
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm web python manage.py migrate
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm web python manage.py collectstatic --noinput
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
```

Provision `btree_gist` through a migration-capable database role. Use a separate least-privilege runtime role in production. The packaged development database role can create the pytest database; do not give production runtime roles that permission. Run only one Beat instance. Gunicorn runs as an unprivileged user, and WhiteNoise serves collected Admin static assets. The production overlay removes source bind mounts and database host exposure. Place Redis and PostgreSQL on a trusted private network; managed services should use authenticated/TLS connections as appropriate.

Configure your trusted TLS proxy explicitly before enabling forwarded HTTPS headers; the application deliberately does not trust arbitrary forwarded headers or client IPs. Without that configuration, TLS termination forwarding plain HTTP will redirect repeatedly. `SECURE_PROXY_SSL_HEADER` can be enabled with `TRUST_PROXY_HTTPS=true` only when the proxy strips/replaces incoming `X-Forwarded-Proto` and clients cannot bypass it. IP allowlists currently use the socket peer, not untrusted `X-Forwarded-For`; restrict direct network access and enforce original-client allowlists at the trusted proxy if necessary.

The deployment check intentionally reports `security.W021` because browser HSTS preload enrollment is not enabled automatically. Run `python manage.py check --deploy --settings=config.settings.production` inside Compose before deployment. Set HSTS policy deliberately. Back up encryption keys separately from database backups. Restore keys and database together. Rotate provider passwords through Room Admin; room token keys include credential version. Retain previous master keys while encrypted historical records are migrated.

## Operational limits in this pass

- One simultaneous booking per room is deliberately enforced by PostgreSQL; values greater than one are rejected.
- IMMEDIATE provisioning is implemented. JIT and MANUAL are modeled for future work but are not exposed as selectable API modes.
- Scheduled Convay creation is gated pending timestamp-field confirmation. LMS scheduled classes using instant Convay meetings are supported.
- No Convay reconciliation, termination, join-link, or refresh endpoint has been invented. Operators verify remote state and use the audited “Confirm provider ended/absent” action before releasing created or ambiguous meetings. LMS cancellation is local and rejects those states.
- Confirmed creation behavior uses Convay `title` and accepts `calendarId`, a non-empty scheme-less panel domain such as `meet.convay.com`, and a trusted HTTPS `startMeetingUrl`. Unconfirmed provider behavior remains listed in the contract notes.
- Convay start URLs must use HTTPS and a hostname covered by `CONVAY_TRUSTED_HOST_SUFFIXES` (default `convay.com`); subdomains such as `meet.convay.com` are accepted.
- A process crash during creation leaves state-based duplicate protection in place. Stale provisioning is marked unknown by Beat and requires reconciliation before another provider creation.
- Rate limiting is Redis-backed per socket IP. Put credential brute-force protection and request-size limits at the ingress too. Audit retention, secret-manager integration, and monitoring policies are deployment responsibilities.
