from django.contrib import admin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "timestamp",
        "actor_type",
        "action",
        "object_type",
        "object_id",
        "request_id",
    )
    search_fields = ("object_id", "request_id", "action")
    list_filter = ("action", "actor_type")

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
