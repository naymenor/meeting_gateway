from common.middleware import request_context
from .models import AuditLog


def audit(action, obj, client=None, admin=None, provider_error=None):
    context = request_context.get()
    return AuditLog.objects.create(
        actor_type="ADMIN" if admin else "CLIENT" if client else "SYSTEM",
        admin_user=admin,
        integration_client=client,
        action=action,
        object_type=obj._meta.label,
        object_id=str(obj.pk),
        request_id=context.get("request_id", ""),
        ip=context.get("ip"),
        metadata={
            "provider_error_code": provider_error.code,
            "upstream_status_code": provider_error.status_code,
            "outcome_unknown": provider_error.ambiguous,
        }
        if provider_error
        else {},
    )
