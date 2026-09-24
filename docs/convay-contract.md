# Convay contract and outstanding confirmation

Only these provider endpoints are established by the supplied requirements:

| Operation | Method | Path |
|---|---|---|
| Authenticate | POST | `/services/vcmeetingsettings/user/authenticate` |
| Create meeting | POST | `/services/vcmeetingsettings/api-user/start-meeting` |

The base defaults to `https://convay.com`. Paths can be overridden with `CONVAY_AUTH_PATH` and `CONVAY_START_PATH`. Protected creation uses `Authorization: Bearer ...`. HTTP redirects are not followed, avoiding forwarding secrets to a redirected destination. Configure only trusted HTTPS provider endpoints.

Authentication sends `username` and `password`. Responses must signal `success: true`. Supported `data` shapes are a token string, a JSON-encoded object, or an object containing `accessToken` and optional `refreshToken`. Unverified JWT `exp` is only a cache TTL hint; the adapter does not use unverified claims for authorization. Opaque tokens use a 60-second cache. JWT cache entries expire at most 300 seconds later and at least 30 seconds before indicated expiration. This cannot detect provider revocation; a confirmed start-meeting 401 invalidates the cached token, authenticates again, and retries creation exactly once. A second 401 fails with `PROVIDER_AUTHENTICATION_ERROR`. Authentication-endpoint 401 responses are not retried.

Creation sends the validated preset plus Convay's required `title` field, mapped from the stored Gateway `Meeting.meeting_title`. The LMS-facing field remains `meetingTitle`; that key is never sent to Convay. The adapter accepts a direct response object or a `success: true` envelope whose object or JSON-encoded `data` contains required `calendarId`, `meetingPanelAddress`, and `startMeetingUrl`, plus optional `uniqueId`. The calendar ID is never reused as a unique ID. Start URLs are encrypted and returned only alongside a scope-authorized token.

The confirmed live response may be returned directly or inside the documented success envelope:

```json
{
  "calendarId": "ac12000f-a08f-1cc7-81a0-cebf67ea293e",
  "meetingPanelAddress": "meet.convay.com",
  "startMeetingUrl": "https://meet.convay.com/example?jwt=fake"
}
```

`calendarId` and `meetingPanelAddress` must be non-empty strings. The panel address is provider-returned metadata and may be a scheme-less domain; it is not restricted to one hostname. `startMeetingUrl` is required, must be HTTPS, must not contain URL userinfo, and its hostname must equal or be a subdomain of one of the comma-separated `CONVAY_TRUSTED_HOST_SUFFIXES` values (default: the `CONVAY_BASE_URL` hostname, normally `convay.com`). Query parameters are allowed because the provider places meeting grants there, but the complete URL remains encrypted at rest and redacted from logs.

The supplied request is not a complete provider API document. These assumptions need confirmation using approved non-production credentials before live deployment:

1. Whether alternate response envelopes occur and whether `uniqueId` is returned. The live direct response and `title` request field are confirmed.
2. Preset config types follow the supplied Convay field definitions: `PASSWORD` and `ALLOW_COUNTRY` are booleans, as are the other Boolean config flags. `PRTCPNTS_LIST` accepts `host` or `all`; `REJOIN_POPUP` and `SHR_OVERLAY` accept `host`, `all`, or `none`. Top-level `meetingType` accepts `instant` or `scheduled`; `preDefineHostEnabled`, `uniqueParticipantJoin`, and `bigMeeting` are booleans. Unknown fields and values of the wrong type are rejected without modifying payload values.
3. Required timestamp fields and timestamp encoding for `meetingType=scheduled`. Gateway stores that preset selection but rejects creation with `PROVIDER_CONTRACT_UNCONFIRMED` until supported. No timestamps or undocumented field names are invented.
4. Whether panel URLs contain query parameters or sensitive grants, and exact start URL semantics.
5. Token lifetime, revocation behavior, account versus meeting scope, and any supported refresh semantics. Tokens must be treated as account-scoped until confirmed otherwise. Independent LMS clients sharing room accounts could receive authority over each other's remote meetings even though Gateway API data is isolated. Only onboard clients trusted for that shared account authority, or provision separate Gateway/account pools.
6. Actual active-meeting plan limits. No default limit is invented; configured limits are conservative unresolved-meeting counts.
7. Whether the provider supports idempotency, lookup, termination, or reconciliation. No such endpoint is assumed or called.

## Failure classification and safe diagnostics

| Upstream outcome | Gateway error code | New creation reservation |
|---|---|---|
| 400 / 422 | `PROVIDER_VALIDATION_ERROR` | FAILED; released |
| 401 after one re-authentication/retry | `PROVIDER_AUTHENTICATION_ERROR` | FAILED; released |
| 403 | `PROVIDER_AUTHORIZATION_ERROR` | FAILED; released |
| 404 | `PROVIDER_ENDPOINT_ERROR` | FAILED; released |
| 429 | `PROVIDER_RATE_LIMITED` | FAILED; released |
| 5xx during start-meeting | `PROVIDER_STATE_UNKNOWN` | Unknown; retained |
| Connect timeout/error or connection-pool timeout before sending | `PROVIDER_UNAVAILABLE` | FAILED; released |
| Read/write timeout, connection drop or other uncertain transport failure during creation | `PROVIDER_STATE_UNKNOWN` | Unknown; retained |
| Malformed successful creation response | malformed-response error | Unknown; retained |

If HTTP 200 includes a valid `calendarId` but later local validation fails, creation is known to have succeeded. Gateway stores the available provider identifiers, retains the reservation, and uses `PROVIDER_RESPONSE_INVALID` rather than `PROVIDER_STATE_UNKNOWN`. It never retries that meeting automatically.

Gateway maps `PROVIDER_VALIDATION_ERROR` to HTTP 422 for the LMS because Convay definitively rejected the supplied provider representation. Rate limiting and pre-send unavailability map to HTTP 503. HTTP 502 is reserved for provider gateway/contract/authentication failures and ambiguous creation outcomes.

Only a confirmed start-meeting 401 triggers automatic re-authentication and one retry. No automatic retries occur for validation errors, rate limiting, connect errors, ambiguous failures, or existing unknown meetings. New definite failures transition to FAILED. Existing unknown reservations are not reclassified or released automatically. Operators may either confirm the provider ended/does not exist or recover the meeting in Admin using the confirmed calendar ID, panel address, and trusted HTTPS start URL.

Every failed provider HTTP attempt emits `convay.request_failed` with `request_id` (also `requestId` for compatibility), operation, HTTP method, endpoint path without query/host, upstream status, exception class, safe upstream error diagnostics, room public ID, Gateway Meeting UUID when available, and elapsed milliseconds. JSON diagnostic fields such as `status`, `message`, `fieldErrors`, `errors`, and `code` are retained with recursive sensitive-key redaction, known-secret scrubbing, and URL/JWT/Bearer removal. Provider `stackTrace`/trace fields, non-diagnostic scalar values, raw exception messages, request bodies, and headers are omitted. Diagnostic body size, nesting, arrays, and string lengths are bounded. Audit events store only the internal error code, upstream status, and ambiguity flag, never upstream bodies.

Temporary live-contract diagnostics also emit `convay.start_meeting.request` immediately before the HTTP call and `convay.start_meeting.response` immediately after an HTTP response. The request event contains the final merged JSON payload, Gateway Meeting UUID, room public ID, method, and endpoint path. The response event contains the upstream status and sanitized JSON or bounded sanitized text. Authorization headers and provider/account secrets are never included. The documented Boolean `config.PASSWORD` flag is retained because it is configuration rather than a credential; any string-valued password field is redacted. Remove or downgrade these temporary payload events after provider contract verification if production log volume is undesirable.

Match the response's `X-Request-ID` or error `requestId` to the structured logs:

```bash
docker compose logs --since=15m web
```

Request IDs are generated before security/static middleware and retained on the Django request, so Django's response logs remain correlated after middleware context cleanup. Error replays use the current request ID. This does not recover the previously unrecorded upstream error body and does not resend the historical failed request.

The LMS uses its existing Convay integration for later calls and unique teacher/student join-link creation. Gateway never returns provider username, password, refresh token, encrypted credential, or master key.
