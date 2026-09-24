import uuid
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models


class Meeting(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT"
        RESERVED = "RESERVED"
        PROVISIONING = "PROVISIONING"
        READY = "READY"
        LIVE = "LIVE"
        ENDED = "ENDED"
        CANCELLED = "CANCELLED"
        FAILED = "FAILED"
        PROVIDER_RESPONSE_INVALID = "PROVIDER_RESPONSE_INVALID"
        PROVIDER_STATE_UNKNOWN = "PROVIDER_STATE_UNKNOWN"

    class ScheduleType(models.TextChoices):
        SCHEDULED = "SCHEDULED"
        INSTANT = "INSTANT"

    class ProviderMeetingType(models.TextChoices):
        INSTANT = "INSTANT"
        SCHEDULED = "SCHEDULED"

    class ProvisionStrategy(models.TextChoices):
        IMMEDIATE = "IMMEDIATE"
        JIT = "JIT"
        MANUAL = "MANUAL"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    integration_client = models.ForeignKey(
        "integrations.IntegrationClient", on_delete=models.PROTECT
    )
    external_class_id = models.CharField(max_length=160, db_index=True)
    teacher_id = models.CharField(max_length=160, db_index=True)
    teacher_name = models.CharField(max_length=200)
    subject_id = models.CharField(max_length=160, blank=True, db_index=True)
    subject_name = models.CharField(max_length=200, blank=True)
    batch_id = models.CharField(max_length=160, db_index=True)
    batch_name = models.CharField(max_length=200)
    meeting_title = models.CharField(max_length=250)
    class_date = models.DateField(db_index=True)
    schedule_type = models.CharField(
        max_length=16, choices=ScheduleType.choices, default=ScheduleType.SCHEDULED
    )
    start_at = models.DateTimeField(null=True, db_index=True)
    end_at = models.DateTimeField(null=True, db_index=True)
    room = models.ForeignKey("rooms.Room", null=True, on_delete=models.PROTECT)
    reservation = DateTimeRangeField(null=True)
    reservation_active = models.BooleanField(default=False)
    provider = models.CharField(max_length=16, default="CONVAY", editable=False)
    provider_meeting_type = models.CharField(
        max_length=16,
        choices=ProviderMeetingType.choices,
        default=ProviderMeetingType.INSTANT,
    )
    provision_strategy = models.CharField(
        max_length=16,
        choices=ProvisionStrategy.choices,
        default=ProvisionStrategy.IMMEDIATE,
    )
    provider_calendar_id = models.CharField(max_length=200, null=True, db_index=True)
    provider_unique_id = models.CharField(max_length=200, null=True)
    provider_panel_address = models.CharField(max_length=2000, null=True)
    encrypted_provider_start_url = models.TextField(null=True, editable=False)
    encrypted_provider_payload = models.TextField(null=True, editable=False)
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    provider_created_at = models.DateTimeField(null=True)
    last_provider_error = models.CharField(max_length=100, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration_client", "external_class_id"],
                name="unique_client_external_class",
            ),
            ExclusionConstraint(
                name="exclude_overlapping_room_reservations",
                expressions=[
                    ("room", RangeOperators.EQUAL),
                    ("reservation", RangeOperators.OVERLAPS),
                ],
                condition=models.Q(reservation_active=True),
            ),
            models.CheckConstraint(
                condition=models.Q(reservation_active=False)
                | (
                    models.Q(room__isnull=False)
                    & models.Q(reservation__isnull=False)
                    & models.Q(start_at__isnull=False)
                    & models.Q(end_at__isnull=False)
                ),
                name="active_reservation_complete",
            ),
            models.CheckConstraint(
                condition=models.Q(start_at__isnull=True, end_at__isnull=True)
                | models.Q(end_at__gt=models.F("start_at")),
                name="positive_meeting_interval",
            ),
        ]


class IdempotencyRecord(models.Model):
    client = models.ForeignKey(
        "integrations.IntegrationClient", on_delete=models.CASCADE
    )
    key = models.CharField(max_length=128)
    operation = models.CharField(max_length=200)
    payload_hash = models.CharField(max_length=64)
    meeting = models.ForeignKey(Meeting, null=True, on_delete=models.PROTECT)
    encrypted_result = models.TextField(null=True, editable=False)
    response_status = models.PositiveSmallIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "key"], name="unique_client_idempotency_key"
            )
        ]
