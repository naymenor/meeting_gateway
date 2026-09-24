# LMS integration workflow

Swagger at `/api/docs/` is the authoritative endpoint contract, including
request fields, response schemas, scopes, examples, and error codes. The LMS
workflow is:

1. Exchange the Integration Client `client_id` and `client_secret` at
   `POST /api/v1/auth/token/`.
2. Send the returned Gateway JWT as `Authorization: Bearer <token>`.
3. Register class metadata with `POST /api/v1/meetings/`. A client may have
   only one meeting for a given `class.id`; repeating registration returns
   that meeting.
4. Choose a room and free slot from the registration response or
   `GET /api/v1/rooms/availability/`.
5. Create it with `POST /api/v1/meetings/{id}/create/`. A READY meeting is
   returned without creating another Convay meeting. In-progress and ambiguous
   provider outcomes are blocked from blind retry.
6. Store the Gateway Meeting ID and Convay `calendarId`, then continue with
   the trusted server-side Convay integration where required.
7. Request a current account token with
   `POST /api/v1/meetings/{id}/convay-token/`.
8. Cancel eligible local meetings with
   `POST /api/v1/meetings/{id}/cancel/`; repeating a completed cancellation
   returns the current CANCELLED meeting.

The LMS does not send an `Idempotency-Key`. Registration uniqueness and
meeting state transitions provide duplicate protection. Room availability is a
snapshot; PostgreSQL rechecks and enforces the slot atomically during creation.

Convay access tokens may authorize the room account rather than one meeting.
They are for the trusted LMS backend only and must never be exposed to browser
or mobile client code. Gateway never returns provider usernames, passwords,
refresh tokens, client secrets, or encryption keys.

Liveness is `GET /health/live/`; readiness is `GET /health/ready/`.
Readiness checks PostgreSQL and Redis without contacting Convay.
