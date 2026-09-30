from datetime import timedelta, timezone as datetime_timezone
from cryptography.fernet import InvalidToken
from django.core.exceptions import ValidationError
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from psycopg.types.range import Range
from apps.audit.services import audit
from apps.meetings.models import Meeting
from common.exceptions import GatewayError
from common.encryption import decrypt
from .configuration import resolve_config
from .models import Room


def interval(start, end):
    if timezone.is_naive(start) or timezone.is_naive(end) or start >= end:
        raise GatewayError(
            "INVALID_INTERVAL",
            "Provide timezone-aware start and end with end after start.",
        )
    try:
        return Range(
            start - timedelta(minutes=settings.ROOM_BUFFER_BEFORE_MINUTES),
            end + timedelta(minutes=settings.ROOM_BUFFER_AFTER_MINUTES),
            "[)",
        )
    except OverflowError:
        raise GatewayError("INVALID_INTERVAL", "Interval exceeds supported date bounds.") from None


def configured(room, client):
    """Local eligibility only; never authenticate with Convay for a snapshot."""
    if not room.is_active or room.max_concurrent_bookings != 1 or not room.username.strip():
        return False
    if not room.encrypted_password:
        return False
    try:
        if not decrypt(room.encrypted_password):
            return False
        return resolve_config(client, room)["meetingType"] == "instant"
    except (InvalidToken, ValueError, KeyError, ValidationError):
        return False


def capacity_error(room, slot):
    if Meeting.objects.filter(
        room=room, reservation_active=True, reservation__overlap=slot
    ).exists():
        return "ROOM_SLOT_CONFLICT"
    if (
        room.provider_active_meeting_limit is not None
        and Meeting.objects.filter(
            room=room,
            status__in=["RESERVED", "PROVISIONING", "READY", "LIVE",
                        "PROVIDER_RESPONSE_INVALID", "PROVIDER_STATE_UNKNOWN"],
        ).count() >= room.provider_active_meeting_limit
    ):
        return "PROVIDER_CAPACITY"
    return None



class RoomAllocator:
    @staticmethod
    def reserve_specific_room(meeting, room_id, start, end):
        slot = interval(start, end)
        try:
            with transaction.atomic():
                locked = Meeting.objects.select_for_update().get(pk=meeting.pk)
                if locked.status != Meeting.Status.DRAFT:
                    raise GatewayError(
                        "INVALID_STATE",
                        "Meeting is already reserved or finalized.",
                        409,
                    )
                room = (
                    Room.objects.select_for_update()
                    .filter(public_id=room_id, is_active=True)
                    .first()
                )
                if not room or not configured(room, locked.integration_client):
                    raise GatewayError(
                        "ROOM_UNAVAILABLE", "Room does not exist or is inactive.", 409
                    )
                conflict = capacity_error(room, slot)
                if conflict:
                    raise GatewayError(conflict, "Internal capacity is unavailable.", 409)
                locked.room = room
                locked.start_at, locked.end_at = start, end
                locked.reservation, locked.reservation_active = slot, True
                locked.status = Meeting.Status.RESERVED
                locked.save()
                audit("slot_reserved", locked, client=locked.integration_client)
                return locked
        except IntegrityError as exc:
            if getattr(getattr(exc, "__cause__", None), "sqlstate", None) != "23P01":
                raise
            raise GatewayError(
                "ROOM_SLOT_CONFLICT",
                "The selected room is no longer available for this time.",
                409,
            ) from None

    @classmethod
    def allocate_any_room(cls, meeting, start, end):
        for public_id in Room.objects.filter(is_active=True).order_by("priority", "public_id").values_list(
            "public_id", flat=True
        ):
            try:
                return cls.reserve_specific_room(meeting, public_id, start, end)
            except GatewayError as exc:
                if exc.default_code not in (
                    "ROOM_SLOT_CONFLICT",
                    "ROOM_UNAVAILABLE",
                    "PROVIDER_CAPACITY",
                ):
                    raise
        raise GatewayError(
            "NO_CAPACITY_AVAILABLE", "No meeting capacity is available for the requested time.", 409
        )


def availability(client, start, end):
    slot = interval(start, end)
    available = any(
        configured(room, client) and capacity_error(room, slot) is None
        for room in Room.objects.filter(is_active=True).order_by("priority", "public_id")
    )
    return {
        "startAt": start.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z"),
        "endAt": end.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z"),
        "available": available,
    }
