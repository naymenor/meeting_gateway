from django.contrib import admin
from django import forms
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from urllib.parse import urlsplit
from apps.audit.services import audit
from common.encryption import encrypt
from .models import Meeting


class MeetingRecoveryForm(forms.ModelForm):
    recovery_start_meeting_url = forms.URLField(
        required=False,
        assume_scheme="https",
        help_text=(
            "For PROVIDER_STATE_UNKNOWN recovery only. Stored encrypted and never "
            "displayed again."
        ),
    )

    class Meta:
        model = Meeting
        fields = "__all__"

    def clean(self):
        values = super().clean()
        if not self.instance.pk:
            return values
        original = Meeting.objects.get(pk=self.instance.pk)
        if original.status != Meeting.Status.PROVIDER_STATE_UNKNOWN:
            return values
        calendar_id = (values.get("provider_calendar_id") or "").strip()
        panel = (values.get("provider_panel_address") or "").strip()
        start_url = (values.get("recovery_start_meeting_url") or "").strip()
        if not calendar_id or not panel or not start_url:
            raise forms.ValidationError(
                "Recovery requires calendarId, meetingPanelAddress, and startMeetingUrl."
            )
        if (
            original.provider_calendar_id
            and original.provider_calendar_id != calendar_id
        ):
            raise forms.ValidationError(
                "Recovery cannot overwrite a different existing calendarId."
            )
        parsed = urlsplit(start_url)
        trusted = parsed.hostname and any(
            parsed.hostname == suffix or parsed.hostname.endswith("." + suffix)
            for suffix in settings.CONVAY_TRUSTED_HOST_SUFFIXES
        )
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or not trusted
        ):
            raise forms.ValidationError(
                "startMeetingUrl must be HTTPS on a trusted Convay domain."
            )
        return values


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    form = MeetingRecoveryForm
    list_display = (
        "id",
        "external_class_id",
        "meeting_title",
        "room",
        "class_date",
        "start_at",
        "status",
    )
    search_fields = (
        "id",
        "external_class_id",
        "teacher_id",
        "teacher_name",
        "batch_id",
        "room__public_id",
        "provider_calendar_id",
    )
    list_filter = ("status", "class_date", "room", "integration_client")
    date_hierarchy = "class_date"
    actions = ["confirm_provider_ended"]

    def get_fields(self, request, obj=None):
        fields = [
            f.name for f in Meeting._meta.fields if not f.name.startswith("encrypted_")
        ]
        if obj and obj.status == Meeting.Status.PROVIDER_STATE_UNKNOWN:
            fields.append("recovery_start_meeting_url")
        return fields

    def get_readonly_fields(self, request, obj=None):
        fields = self.get_fields(request, obj)
        if obj and obj.status == Meeting.Status.PROVIDER_STATE_UNKNOWN:
            editable = {
                "provider_calendar_id",
                "provider_panel_address",
                "recovery_start_meeting_url",
            }
            return [field for field in fields if field not in editable]
        return fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            original = (
                Meeting.objects.select_for_update().get(pk=obj.pk) if change else None
            )
            if (
                original
                and original.status == Meeting.Status.PROVIDER_STATE_UNKNOWN
            ):
                if (
                    original.provider_calendar_id
                    and original.provider_calendar_id != obj.provider_calendar_id
                ):
                    raise forms.ValidationError(
                        "Recovery cannot overwrite a different existing calendarId."
                    )
                obj.encrypted_provider_start_url = encrypt(
                    form.cleaned_data["recovery_start_meeting_url"]
                )
                obj.provider_created_at = obj.provider_created_at or timezone.now()
                obj.status = Meeting.Status.READY
                obj.reservation_active = True
                obj.last_provider_error = None
                super().save_model(request, obj, form, change)
                audit("admin_recovered_provider_meeting", obj, admin=request.user)
                return
            super().save_model(request, obj, form, change)

    @admin.action(
        description="Confirm provider ended/absent and release reservation",
        permissions=["change"],
    )
    def confirm_provider_ended(self, request, queryset):
        with transaction.atomic():
            for meeting in queryset.select_for_update():
                if meeting.status not in (
                    "READY",
                    "LIVE",
                    "PROVIDER_RESPONSE_INVALID",
                    "PROVIDER_STATE_UNKNOWN",
                ):
                    continue
                meeting.status = Meeting.Status.ENDED
                meeting.reservation_active = False
                meeting.save()
                audit("admin_confirmed_provider_ended", meeting, admin=request.user)
