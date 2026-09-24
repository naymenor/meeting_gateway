from django import forms
from django.contrib import admin
from django.db import transaction
from django.utils import timezone
from apps.audit.services import audit
from apps.convay.tokens import get_token
from apps.convay.client import ProviderError
from apps.meetings.models import Meeting
from .models import Room, MeetingConfigPreset


class RoomForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Write-only. Leave blank to retain the configured credential.",
    )

    class Meta:
        model = Room
        exclude = ("encrypted_password",)

    def clean(self):
        values = super().clean()
        if not self.instance.pk or not self.instance.encrypted_password:
            if not values.get("password"):
                self.add_error("password", "A password is required for a new room.")
        if self.instance.pk and "username" in values:
            previous = Room.objects.filter(pk=self.instance.pk).first()
            if (
                previous
                and previous.username != values["username"]
                and Meeting.objects.filter(room=previous).exists()
            ):
                self.add_error(
                    "username",
                    "Accounts with meeting history cannot be reassigned. Create a new room.",
                )
        return values


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    form = RoomForm
    list_display = (
        "public_id",
        "name",
        "credential_configured",
        "is_active",
        "priority",
        "booking_count",
        "last_auth_at",
        "last_auth_success",
    )
    search_fields = ("public_id", "name")
    readonly_fields = (
        "credential_configured",
        "credential_version",
        "last_auth_at",
        "last_auth_success",
        "last_error",
    )
    actions = ["activate", "deactivate", "test_credential"]

    def get_list_display(self, request):
        fields = super().get_list_display(request)
        return fields + ("username",) if request.user.is_superuser else fields

    def get_fields(self, request, obj=None):
        fields = [
            "public_id",
            "name",
            "is_active",
            "priority",
            "max_concurrent_bookings",
            "provider_active_meeting_limit",
            "credential_configured",
            "last_auth_at",
            "last_auth_success",
            "last_error",
        ]
        if request.user.is_superuser:
            fields += [
                "username",
                "password",
                "credential_version",
                "default_meeting_config",
            ]
        return fields

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if not request.user.is_superuser:
            form.base_fields.pop("password", None)
        return form

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Credential")
    def credential_configured(self, obj):
        return "Configured" if obj.encrypted_password else "Not configured"

    def booking_count(self, obj):
        return Meeting.objects.filter(
            room=obj, reservation_active=True, end_at__gte=timezone.now()
        ).count()

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            previous = Room.objects.select_for_update().filter(pk=obj.pk).first()
            if (
                previous
                and previous.username != obj.username
                and Meeting.objects.filter(room=previous).exists()
            ):
                raise forms.ValidationError(
                    "An account with meeting history cannot be reassigned; create a new room."
                )
            password = form.cleaned_data.get("password")
            if password and request.user.is_superuser:
                obj.credential_version = previous.credential_version if previous else 1
                obj.set_password(password)
            super().save_model(request, obj, form, change)
            audit(
                "credential_changed"
                if password
                else "room_updated"
                if change
                else "room_created",
                obj,
                admin=request.user,
            )

    def set_active(self, request, queryset, value):
        with transaction.atomic():
            for obj in queryset.select_for_update():
                obj.is_active = value
                obj.save(update_fields=["is_active", "updated_at"])
                audit(
                    "room_activated" if value else "room_deactivated",
                    obj,
                    admin=request.user,
                )

    @admin.action(description="Activate rooms", permissions=["change"])
    def activate(self, request, queryset):
        self.set_active(request, queryset, True)

    @admin.action(description="Deactivate rooms", permissions=["change"])
    def deactivate(self, request, queryset):
        self.set_active(request, queryset, False)

    @admin.action(description="Test Convay credentials", permissions=["change"])
    def test_credential(self, request, queryset):
        for obj in queryset:
            try:
                get_token(obj, force=True)
                self.message_user(
                    request, f"{obj.public_id}: authentication succeeded."
                )
            except ProviderError:
                self.message_user(
                    request, f"{obj.public_id}: authentication failed.", level="ERROR"
                )
            audit("credential_tested", obj, admin=request.user)


@admin.register(MeetingConfigPreset)
class PresetAdmin(admin.ModelAdmin):
    list_display = ("name", "is_system_default")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    has_add_permission = has_view_permission
    has_change_permission = has_view_permission
    has_delete_permission = has_view_permission

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit("preset_updated", obj, admin=request.user)
