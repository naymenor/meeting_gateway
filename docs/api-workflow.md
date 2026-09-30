# LMS integration workflow

Swagger at `/api/docs/` is the primary live contract for fields, responses,
scopes, examples and errors. Documentation access requires a staff session.

1. Exchange `client_id` and `client_secret` at `POST /api/v1/auth/token/`.
2. Send the Gateway JWT as `Authorization: Bearer <token>`.
3. Call `GET /api/v1/rooms/availability/?date=2026-09-30` (`room:read`).
   Optional `start_at` and `end_at` query parameters check a particular interval.
4. Let the user select a Room and time.
5. Send metadata and the selected interval to `POST /api/v1/meetings/`
   (`meeting:write`):

```json
{
  "meetingTitle": "Physics Class",
  "teacher": {"id": "T-001", "name": "Test Teacher"},
  "batch": {"id": "B-001", "name": "Test Batch"},
  "class": {"id": "CLS-001", "date": "2026-09-30"},
  "roomId": "ROOM-01",
  "startAt": "2026-09-30T17:00:00+06:00",
  "endAt": "2026-09-30T18:00:00+06:00"
}
```

6. Gateway atomically rechecks availability and reserves the selected slot,
   authenticates the Room's Convay account, creates the provider meeting and
   persists the response. Availability is only a snapshot, never a reservation.
7. Receive HTTP 201 with the completed READY meeting, including `classInfo`,
   `meetingInfo`, `roomInfo`, `convay` and `createdAt`. Authorization and the
   sensitive start URL are included only with `meeting:token`.
8. Store the Gateway UUID and provider `calendarId`. Later call
   `POST /api/v1/meetings/{uuid}/convay-token/` (`meeting:token`) to obtain a
   current account token. This repeatable endpoint verifies ownership and
   reuses a valid cached Room token or authenticates again.

All seven top-level creation fields above are required. Timestamps must carry
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
| Selected slot taken | 409 `ROOM_SLOT_CONFLICT`; refresh availability and select another slot |

A slot conflict leaves an internal DRAFT that can be retried with another slot
for the same class. The original metadata is retained when a class is reused.
Provider validation failures return 422, rate limiting or pre-send unavailability
503, and other provider failures 502. Ambiguous creation retains the reservation
and requires Admin reconciliation; never blindly retry provider creation.
There is no LMS cancellation operation in V1.

List and retrieve owned meetings with `GET /api/v1/meetings/` and
`GET /api/v1/meetings/{uuid}/` (`meeting:read`). These omit tokens and start URLs.
Convay access tokens may authorize the whole Room account. Keep them on the
trusted LMS server; never expose them to browsers or mobile clients. Gateway
never returns provider usernames, passwords, refresh tokens or encryption keys.

Liveness is `GET /health/live/`; readiness is `GET /health/ready/`.
Readiness checks PostgreSQL and Redis without contacting Convay.
