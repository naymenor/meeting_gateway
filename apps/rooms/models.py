import uuid
from django.core.exceptions import ValidationError
from django.db import models
from common.encryption import encrypt
from .configuration import validate_config


class MeetingConfigPreset(models.Model):
    name = models.CharField(max_length=160, unique=True)
    payload = models.JSONField(default=dict, validators=[validate_config])
    is_system_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["is_system_default"],
                condition=models.Q(is_system_default=True),
                name="one_system_preset",
            )
        ]

    def __str__(self):
        return self.name


class Room(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=160)
    username = models.CharField(max_length=254, unique=True)
    encrypted_password = models.TextField(editable=False)
    credential_version = models.PositiveIntegerField(default=1, editable=False)
    is_active = models.BooleanField(default=True)
    priority = models.IntegerField(default=100)
    max_concurrent_bookings = models.PositiveSmallIntegerField(default=1)
    provider_active_meeting_limit = models.PositiveIntegerField(null=True, blank=True)
    default_meeting_config = models.JSONField(
        default=dict, blank=True, validators=[validate_config]
    )
    last_auth_at = models.DateTimeField(null=True, editable=False)
    last_auth_success = models.BooleanField(null=True, editable=False)
    last_error = models.CharField(max_length=100, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "public_id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_concurrent_bookings=1),
                name="single_booking_per_room",
            ),
            models.CheckConstraint(
                condition=models.Q(provider_active_meeting_limit__isnull=True)
                | models.Q(provider_active_meeting_limit__gt=0),
                name="positive_provider_limit",
            ),
        ]

    def set_password(self, value):
        self.encrypted_password = encrypt(value)
        self.credential_version += 1

    def clean(self):
        if self.max_concurrent_bookings != 1:
            raise ValidationError(
                "This release supports exactly one concurrent booking per room."
            )

    def __str__(self):
        return self.public_id
