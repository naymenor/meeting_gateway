import json
import logging
from django.db import transaction
from django.utils import timezone
from apps.audit.services import audit
from apps.rooms.configuration import resolve_config
from apps.rooms.services import RoomAllocator
from apps.convay.client import ConvayClient, ProviderError
from apps.convay.tokens import get_token, invalidate
from apps.convay.diagnostics import sanitize_text
from common.encryption import encrypt, decrypt
from common.exceptions import GatewayError
from common.middleware import request_context
from .models import Meeting


def register(client, values):
    try:
        meeting, created = Meeting.objects.get_or_create(
            integration_client=client,
            external_class_id=values["class"]["id"],
            defaults={
                "meeting_title": values["meetingTitle"],
                "class_date": values["class"]["date"],
                "teacher_id": values["teacher"]["id"],
                "teacher_name": values["teacher"]["name"],
                "batch_id": values["batch"]["id"],
                "batch_name": values["batch"]["name"],
                "schedule_type": values.get("scheduleType", "SCHEDULED"),
                "start_at": values.get("startAt"),
                "end_at": values.get("endAt"),
            },
        )
    except Meeting.MultipleObjectsReturned:
        # A legacy database may predate the logical-class uniqueness constraint.
        logging.getLogger("gateway.meetings").warning({
            "event": "meeting.logical_duplicates",
            "integration_client_id": str(client.pk),
            "external_class_id": sanitize_text(values["class"]["id"]),
        }, extra={"request_id": request_context.get().get("request_id")})
        raise GatewayError(
            "EXISTING_MEETING_REQUIRES_REVIEW",
            "Existing meeting records require operator review.",
            409,
        ) from None
    if created:
        audit("meeting_draft_created", meeting, client=client)
    else:
        expected = {
            "meeting_title": values["meetingTitle"],
            "teacher_id": values["teacher"]["id"],
            "teacher_name": values["teacher"]["name"],
            "batch_id": values["batch"]["id"],
            "batch_name": values["batch"]["name"],
            "class_date": values["class"]["date"],
        }
        for field, key in (("start_at", "startAt"), ("end_at", "endAt")):
            if getattr(meeting, field) is not None:
                expected[field] = values.get(key)
        if any(getattr(meeting, field) != value for field, value in expected.items()):
            audit("existing_meeting_mismatch", meeting, client=client)
            raise GatewayError("EXISTING_MEETING_MISMATCH", "Original meeting request data differs.", 409)
    return meeting, created


def provision(meeting, values):
    with transaction.atomic():
        locked = Meeting.objects.select_for_update().get(pk=meeting.pk)
        if locked.status in (Meeting.Status.READY, Meeting.Status.LIVE):
            audit("meeting_ready_replayed", locked, client=locked.integration_client)
            return locked, None
        if locked.status in (Meeting.Status.RESERVED, Meeting.Status.PROVISIONING):
            raise GatewayError(
                "CREATION_IN_PROGRESS",
                "Meeting creation is already in progress.",
                409,
            )
        if locked.status in (
            Meeting.Status.PROVIDER_STATE_UNKNOWN,
            Meeting.Status.PROVIDER_RESPONSE_INVALID,
        ):
            raise GatewayError(
                "PROVIDER_RECONCILIATION_REQUIRED",
                "Review the existing provider outcome before creating again.",
                409,
            )
        terminal_errors = {
            Meeting.Status.ENDED: ("CLASS_ALREADY_COMPLETED", "This class has already completed."),
            Meeting.Status.NON_RETRYABLE_FAILED: ("PREVIOUS_CREATION_FAILED", "Previous creation failed; operator review is required."),
            Meeting.Status.FAILED: ("PREVIOUS_CREATION_FAILED", "Previous meeting creation failed; operator review is required."),
            Meeting.Status.CANCELLED: ("CLASS_CANCELLED", "This class has been cancelled."),
        }
        if locked.status in terminal_errors:
            code, message = terminal_errors[locked.status]
            raise GatewayError(code, message, 409)
        retrying = locked.status == Meeting.Status.RETRYABLE_FAILED
        if locked.status not in (Meeting.Status.DRAFT, Meeting.Status.RETRYABLE_FAILED):
            raise GatewayError(
                "EXISTING_MEETING_REQUIRES_REVIEW",
                "The existing meeting requires operator review.",
                409,
            )
        if retrying and any((locked.provider_calendar_id, locked.provider_unique_id,
                             locked.provider_panel_address, locked.encrypted_provider_start_url)):
            raise GatewayError(
                "PROVIDER_RECONCILIATION_REQUIRED",
                "Existing provider fields require review before another creation.",
                409,
            )
        if values["startAt"].date() != locked.class_date:
            raise GatewayError(
                "CLASS_DATE_MISMATCH",
                "The start date in the service timezone must match the class date.",
            )
        if retrying and locked.reservation_active and locked.room_id:
            reserved = locked
        else:
            locked.status = Meeting.Status.DRAFT
            locked.save(update_fields=["status", "updated_at"])
            reserved = RoomAllocator.allocate_any_room(
                locked, values["startAt"], values["endAt"]
            )
        payload = (
            json.loads(decrypt(reserved.encrypted_provider_payload))
            if retrying and reserved.encrypted_provider_payload
            else resolve_config(reserved.integration_client, reserved.room)
        )
        # meetingTitle is the Gateway/LMS field; Convay's contract requires title.
        payload.pop("meetingTitle", None)
        payload["title"] = reserved.meeting_title
        # Provider scheduled timestamps are deliberately gated until contract verified.
        if payload["meetingType"] == "scheduled":
            raise GatewayError(
                "PROVIDER_CONTRACT_UNCONFIRMED",
                "Scheduled provider payload requires confirmed Convay timestamp fields.",
                422,
            )
        reserved.provider_meeting_type = payload["meetingType"].upper()
        reserved.encrypted_provider_payload = encrypt(json.dumps(payload))
        reserved.status = Meeting.Status.PROVISIONING
        reserved.save()
        audit("provider_retry_started" if retrying else "provider_provisioning_started",
              reserved, client=reserved.integration_client)
    client = ConvayClient(meeting_id=reserved.pk)
    try:
        token = get_token(reserved.room, meeting_id=reserved.pk)
        try:
            data = client.start_meeting(reserved.room, payload, token.access_token)
        except ProviderError as exc:
            if exc.status_code != 401:
                raise
            # A confirmed 401 rejects the request. Re-authenticate and retry once.
            invalidate(reserved.room)
            token = get_token(reserved.room, force=True, meeting_id=reserved.pk)
            data = client.start_meeting(reserved.room, payload, token.access_token)
    except ProviderError as exc:
        if exc.status_code == 401:
            invalidate(reserved.room)
        with transaction.atomic():
            current = Meeting.objects.select_for_update().get(pk=reserved.pk)
            if exc.known_created:
                current.status = Meeting.Status.PROVIDER_RESPONSE_INVALID
                current.reservation_active = True
                result = exc.provider_result
                current.provider_calendar_id = result.calendar_id
                current.provider_unique_id = result.unique_id
                current.provider_panel_address = result.meeting_panel_address
                current.provider_created_at = timezone.now()
            else:
                current.status = (
                    Meeting.Status.PROVIDER_STATE_UNKNOWN
                    if exc.ambiguous
                    else Meeting.Status.RETRYABLE_FAILED
                    if exc.code in ("PROVIDER_UNAVAILABLE", "PROVIDER_RATE_LIMITED", "PROVIDER_AUTH_BUSY")
                    else Meeting.Status.NON_RETRYABLE_FAILED
                )
                current.reservation_active = exc.ambiguous or current.status == Meeting.Status.RETRYABLE_FAILED
            if retrying:
                audit("provider_retry_failed", current, client=current.integration_client)
            if exc.ambiguous:
                audit("provider_state_unknown", current, client=current.integration_client)
            current.last_provider_error = exc.code
            current.save()
            audit(
                "provider_error",
                current,
                client=current.integration_client,
                provider_error=exc,
            )
        response_status = {
            "PROVIDER_VALIDATION_ERROR": 422,
            "PROVIDER_RATE_LIMITED": 503,
            "PROVIDER_UNAVAILABLE": 503,
        }.get(exc.code, 502)
        message = {
            "PROVIDER_VALIDATION_ERROR": "Convay rejected the meeting payload.",
            "PROVIDER_RATE_LIMITED": "Convay rate limited the request.",
            "PROVIDER_UNAVAILABLE": "Convay is currently unavailable.",
        }.get(exc.code, "Provider authentication or creation failed.")
        if exc.known_created:
            message = (
                "Convay created the meeting, but its response could not be fully "
                "validated; reservation retained."
            )
        elif exc.ambiguous:
            message = "Provider creation outcome is unknown; reservation retained."
        raise GatewayError(exc.code, message, response_status) from None
    finally:
        client.close()
    with transaction.atomic():
        current = Meeting.objects.select_for_update().get(pk=reserved.pk)
        if current.status not in (
            Meeting.Status.PROVISIONING,
            Meeting.Status.PROVIDER_STATE_UNKNOWN,
        ):
            audit(
                "late_provider_response_requires_review",
                current,
                client=current.integration_client,
            )
            raise GatewayError(
                "PROVIDER_RECONCILIATION_REQUIRED",
                "An operator changed the meeting during creation; review provider state.",
                409,
            )
        current.provider_calendar_id = current.provider_calendar_id or data.calendar_id
        current.provider_unique_id = current.provider_unique_id or data.unique_id
        current.provider_panel_address = current.provider_panel_address or data.meeting_panel_address
        current.encrypted_provider_start_url = current.encrypted_provider_start_url or (
            encrypt(data.start_meeting_url) if data.start_meeting_url else None
        )
        current.provider_created_at = timezone.now()
        current.status = Meeting.Status.READY
        current.last_provider_error = None
        current.save()
        audit("meeting_created", current, client=current.integration_client)
        if retrying:
            audit("provider_retry_succeeded", current, client=current.integration_client)
    return current, token


def cancel(meeting):
    with transaction.atomic():
        current = Meeting.objects.select_for_update().get(pk=meeting.pk)
        if current.status == Meeting.Status.CANCELLED:
            return current
        if current.status not in (
            Meeting.Status.DRAFT,
            Meeting.Status.RESERVED,
            Meeting.Status.FAILED,
            Meeting.Status.RETRYABLE_FAILED,
            Meeting.Status.NON_RETRYABLE_FAILED,
        ):
            raise GatewayError(
                "PROVIDER_RECONCILIATION_REQUIRED",
                "Confirm provider termination through operations before releasing this reservation.",
                409,
            )
        current.status = Meeting.Status.CANCELLED
        current.reservation_active = False
        current.cancelled_at = timezone.now()
        current.save()
        audit("meeting_cancelled", current, client=current.integration_client)
        return current


def authorization(token):
    return {
        "accessToken": token.access_token,
        "tokenType": "Bearer",
        "expiresAt": token.expires_at.isoformat() if token.expires_at else None,
    }


def representation(meeting, token=None):
    value = {
        "id": str(meeting.pk),
        "classInfo": {
            "classId": meeting.external_class_id,
            "teacher": {"id": meeting.teacher_id, "name": meeting.teacher_name},
            "batch": {"id": meeting.batch_id, "name": meeting.batch_name},
        },
        "meetingInfo": {
            "meetingTitle": meeting.meeting_title,
            "classDate": meeting.class_date.isoformat(),
            "scheduleType": meeting.schedule_type.lower(),
            "startAt": meeting.start_at.isoformat() if meeting.start_at else None,
            "endAt": meeting.end_at.isoformat() if meeting.end_at else None,
            "status": meeting.status,
        },
        "convay": {
            "meetingType": meeting.provider_meeting_type.lower(),
            "calendarId": meeting.provider_calendar_id,
            "meetingPanelAddress": meeting.provider_panel_address,
        },
        "createdAt": meeting.created_at.isoformat(),
    }
    if token:
        value["convay"]["authorization"] = authorization(token)
        value["convay"]["startMeetingUrl"] = (
            decrypt(meeting.encrypted_provider_start_url)
            if meeting.encrypted_provider_start_url
            else None
        )
    return value
