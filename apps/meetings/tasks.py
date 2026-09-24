from datetime import timedelta
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from apps.audit.services import audit
from .models import Meeting


@shared_task
def flag_stale_provisioning():
    # A worker/process crash may occur after upstream accepted creation.
    with transaction.atomic():
        for meeting in Meeting.objects.select_for_update(skip_locked=True).filter(
            status=Meeting.Status.PROVISIONING,
            updated_at__lt=timezone.now() - timedelta(minutes=10),
        ):
            meeting.status = Meeting.Status.PROVIDER_STATE_UNKNOWN
            meeting.last_provider_error = "PROVISIONING_INTERRUPTED"
            meeting.save()
            audit("provider_state_unknown", meeting)
