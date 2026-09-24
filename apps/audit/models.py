import uuid
from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_type = models.CharField(max_length=16)
    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    integration_client = models.ForeignKey(
        "integrations.IntegrationClient", null=True, on_delete=models.SET_NULL
    )
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=100)
    request_id = models.CharField(max_length=64, blank=True)
    ip = models.GenericIPAddressField(null=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    metadata = models.JSONField(default=dict)
