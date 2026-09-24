from datetime import datetime, time, timedelta
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from psycopg.types.range import Range
from apps.audit.services import audit
from apps.meetings.models import Meeting
from common.exceptions import GatewayError
from .models import Room


def interval(start, end):
    if timezone.is_naive(start) or timezone.is_naive(end) or start >= end:
        raise GatewayError(
            "INVALID_INTERVAL",
            "Provide timezone-aware start and end with end after start.",
        )
    return Range(
        start - timedelta(minutes=settings.ROOM_BUFFER_BEFORE_MINUTES),
        end + timedelta(minutes=settings.ROOM_BUFFER_AFTER_MINUTES),
        "[)",
    )


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
                if not room:
                    raise GatewayError(
                        "ROOM_UNAVAILABLE", "Room does not exist or is inactive.", 409
                    )
                if Meeting.objects.filter(
                    room=room, reservation_active=True, reservation__overlap=slot
                ).exists():
                    raise GatewayError(
                        "ROOM_SLOT_CONFLICT",
                        "The selected room is no longer available for this time.",
                        409,
                    )
                if (
                    room.provider_active_meeting_limit is not None
                    and Meeting.objects.filter(
                        room=room,
                        status__in=[
                            "RESERVED",
                            "PROVISIONING",
                            "READY",
                            "LIVE",
                            "PROVIDER_RESPONSE_INVALID",
                            "PROVIDER_STATE_UNKNOWN",
                        ],
                    ).count()
                    >= room.provider_active_meeting_limit
                ):
                    raise GatewayError(
                        "PROVIDER_CAPACITY",
                        "Configured provider capacity has been reached.",
                        409,
                    )
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
        for public_id in Room.objects.filter(is_active=True).values_list(
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
            "NO_ROOM_CAPACITY", "No room is available for this interval.", 409
        )


def availability(date, start=None, end=None):
    day_start = timezone.make_aware(datetime.combine(date, time.min))
    day_end = day_start + timedelta(days=1)
    query_slot = interval(start, end) if start and end else None
    result = []
    for room in Room.objects.filter(is_active=True):
        bookings = Meeting.objects.filter(
            room=room,
            reservation_active=True,
            reservation__overlap=Range(day_start, day_end, "[)"),
        ).order_by("start_at")
        item = {
            "roomId": room.public_id,
            "roomName": room.name,
            "bookedSlots": [
                {
                    "startAt": m.reservation.lower.isoformat(),
                    "endAt": m.reservation.upper.isoformat(),
                    "status": "BOOKED",
                }
                for m in bookings
            ],
        }
        if query_slot:
            item["available"] = not Meeting.objects.filter(
                room=room, reservation_active=True, reservation__overlap=query_slot
            ).exists()
        result.append(item)
    return result
