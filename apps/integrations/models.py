import ipaddress
import secrets
import uuid
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import models

SCOPES = {
    "meeting:write",
    "meeting:read",
    "meeting:token",
    "room:read",
    "meeting:cancel",
}


class IntegrationClient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    client_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    secret_hash = models.CharField(max_length=256, editable=False)
    token_version = models.UUIDField(default=uuid.uuid4, editable=False)
    is_active = models.BooleanField(default=True)
    scopes = models.JSONField(default=list)
    ip_allowlist = models.JSONField(default=list, blank=True)
    default_preset = models.ForeignKey(
        "rooms.MeetingConfigPreset", null=True, blank=True, on_delete=models.PROTECT
    )
    last_used_at = models.DateTimeField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_authenticated(self):
        return True

    def clean(self):
        if not isinstance(self.scopes, list) or any(
            s not in SCOPES for s in self.scopes
        ):
            raise ValidationError({"scopes": "Unknown scope."})
        try:
            if not isinstance(self.ip_allowlist, list):
                raise ValueError
            for network in self.ip_allowlist:
                ipaddress.ip_network(network)
        except (ValueError, TypeError):
            raise ValidationError(
                {"ip_allowlist": "Use a list of IP addresses or CIDR networks."}
            )

    def rotate_secret(self):
        raw = secrets.token_urlsafe(48)
        self.secret_hash = make_password(raw)
        self.token_version = uuid.uuid4()
        self.save(update_fields=["secret_hash", "token_version", "updated_at"])
        return raw

    def __str__(self):
        return self.name
