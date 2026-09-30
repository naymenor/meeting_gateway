# LMS integration workflow

Swagger at `/api/docs/` is the primary live contract for fields, responses,
scopes, examples and errors. Documentation access requires a staff session.

1. Exchange `client_id` and `client_secret` at `POST /api/v1/auth/token/`.
2. Send the Gateway JWT as `Authorization: Bearer <token>`.
3. Call `GET /api/v1/rooms/availability/` (`room:read`) with required,
   timezone-aware `start_at` and `end_at` query parameters. URL-encode `+` as
   `%2B` when using numeric timezone offsets. There is no date query parameter.
   For example: `?start_at=2026-09-30T10:00:00Z&end_at=2026-09-30T11:00:00Z`.
   The response data is `{"startAt":"2026-09-30T10:00:00Z","endAt":"2026-09-30T11:00:00Z","available":true}`.
   Unavailable capacity returns HTTP 200 with `available: false`.
4. Let the user choose a time interval.
5. Send metadata and the interval to `POST /api/v1/meetings/`
   (`meeting:write`):

```json
{
  "meetingTitle": "Physics Class",
  "teacher": {"id": "T-001", "name": "Test Teacher"},
  "batch": {"id": "B-001", "name": "Test Batch"},
  "class": {"id": "CLS-001", "date": "2026-09-30"},
  "startAt": "2026-09-30T17:00:00+06:00",
  "endAt": "2026-09-30T18:00:00+06:00"
}
```

6. Room/license allocation is handled automatically by the Meeting Gateway.
   Availability is a point-in-time snapshot and is atomically revalidated when
   the meeting is created. Gateway reserves eligible internal capacity,
   authenticates internally, creates the provider meeting and persists the result.
7. Receive HTTP 201 with the completed READY meeting, including `classInfo`,
   `meetingInfo`, `convay` and `createdAt`. Authorization and the
   sensitive start URL are included only with `meeting:token`.
8. Store the Gateway UUID and provider `calendarId`. Later call
   `POST /api/v1/meetings/{uuid}/convay-token/` (`meeting:token`) to obtain a
   current account token. This repeatable endpoint verifies ownership and
   resolves credentials internally, reusing a valid cached token or authenticating again.

All six top-level creation fields above are required. Timestamps must carry
an offset; PostgreSQL stores UTC instants. The start date in the service timezone
must match `class.date`. Unknown fields are rejected. Academic subject data and
provider configuration are not part of the public request.

No `Idempotency-Key` is required. `(IntegrationClient, class.id)` is unique:

| Existing state / outcome | Response |
|---|---|
| READY / LIVE | 200 with existing meeting; no new provider creation |
| RESERVED / PROVISIONING | 409 `CREATION_IN_PROGRESS` |
| PROVIDER_STATE_UNKNOWN / PROVIDER_RESPONSE_INVALID | 409 `PROVIDER_RECONCILIATION_REQUIRED` |
| ENDED | 409 `CLASS_ALREADY_COMPLETED` |
| FAILED | 409 `PREVIOUS_CREATION_FAILED`; operator review required |
| CANCELLED | 409 `CLASS_CANCELLED` |
| Unsupported legacy state / duplicate logical rows | 409 `EXISTING_MEETING_REQUIRES_REVIEW` |
| Capacity exhausted | 409 `NO_CAPACITY_AVAILABLE`; refresh availability and select another time |

A capacity conflict leaves an internal DRAFT that can be retried with another time
for the same class. The original metadata is retained when a class is reused.
Provider validation failures return 422, rate limiting or pre-send unavailability
503, and other provider failures 502. Ambiguous creation retains the reservation
and requires Admin reconciliation; never blindly retry provider creation.
There is no LMS cancellation operation in V1.

List and retrieve owned meetings with `GET /api/v1/meetings/` and
`GET /api/v1/meetings/{uuid}/` (`meeting:read`). These omit tokens and start URLs.
Convay access tokens may authorize the whole provider account. Keep them on the
trusted LMS server; never expose them to browsers or mobile clients. Gateway
never returns provider usernames, passwords, refresh tokens or encryption keys.

Liveness is `GET /health/live/`; readiness is `GET /health/ready/`.
Readiness checks PostgreSQL and Redis without contacting Convay.

Room identifiers, names, ordering, account identity and booking details are internal. Public availability returns only the requested UTC interval and a Boolean; meeting and token responses never include the assigned capacity identity. Availability validates both endpoints and requires end after start; missing, naive, malformed or unsupported parameters return JSON 400.
