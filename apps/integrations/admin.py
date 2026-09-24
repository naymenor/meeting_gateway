from django.contrib import admin
from django.http import HttpResponse
from django.utils.html import format_html
from apps.audit.services import audit
from .models import IntegrationClient


@admin.register(IntegrationClient)
class IntegrationClientAdmin(admin.ModelAdmin):
    fields = (
        "name",
        "client_id",
        "is_active",
        "scopes",
        "ip_allowlist",
        "default_preset",
        "last_used_at",
    )
    readonly_fields = ("client_id", "last_used_at")
    list_display = ("name", "client_id", "is_active", "last_used_at")
    actions = ["rotate_secret"]

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    has_add_permission = has_view_permission
    has_change_permission = has_view_permission

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            obj.one_time_secret = obj.rotate_secret()
        audit("client_updated" if change else "client_created", obj, admin=request.user)

    def secret_response(self, obj, raw):
        response = HttpResponse(
            format_html(
                '<h1>Copy this secret now; it will not be shown again</h1><p>Client ID: {}</p><pre>{}</pre><a href="/admin/integrations/integrationclient/">Return to clients</a>',
                obj.client_id,
                raw,
            )
        )
        response["Cache-Control"] = "no-store"
        return response

    def response_add(self, request, obj, post_url_continue=None):
        return self.secret_response(obj, obj.one_time_secret)

    @admin.action(description="Rotate secret (select exactly one client)")
    def rotate_secret(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one client.", level="ERROR")
            return
        obj = queryset.get()
        raw = obj.rotate_secret()
        audit("client_secret_rotated", obj, admin=request.user)
        return self.secret_response(obj, raw)
