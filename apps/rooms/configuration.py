from django.core.exceptions import ValidationError

BOOLEAN_CONFIG_FIELDS = {
    "MIC_OFF",
    "CAMERA_OFF",
    "PASSWORD",
    "AUTH_USER",
    "ALLOW_COUNTRY",
    "HOST_SCR_SHR",
    "HOST_RECORD_STARTUP",
    "PRTCPNTS_CHAT_CTRL",
    "HIDE_REQ_TO_TALK",
    "PRTCPNTS_RCTN_ENABLED",
    "PRTCPNTS_POLL_ENABLED",
    "WAITING_ROOM",
    "PRTCPNTS_SETTINGS_ENABLED",
    "PRTCPNTS_AUD_ENABLED",
    "PRTCPNTS_VDO_ENABLED",
    "DIS_CHAT_WBNR",
    "DIS_WANT_TO_TALK_WBNR",
}
ENUM_CONFIG_FIELDS = {
    "PRTCPNTS_LIST": ("host", "all"),
    "REJOIN_POPUP": ("host", "all", "none"),
    "SHR_OVERLAY": ("host", "all", "none"),
}
BOOLEAN_TOP_LEVEL_FIELDS = {
    "preDefineHostEnabled",
    "uniqueParticipantJoin",
    "bigMeeting",
}


def validate_boolean(field, value):
    if type(value) is not bool:
        raise ValidationError(f"Invalid value for {field}. Expected a Boolean.")


def validate_enum(field, value, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(
            f"Invalid value for {field}. Allowed values: {', '.join(allowed)}."
        )


def validate_config(value):
    if not isinstance(value, dict):
        raise ValidationError("Configuration must be an object.")
    for key, val in value.items():
        if key == "meetingType":
            validate_enum(key, val, ("instant", "scheduled"))
        elif key in BOOLEAN_TOP_LEVEL_FIELDS:
            validate_boolean(key, val)
        elif key == "config":
            if not isinstance(val, dict):
                raise ValidationError("config must be an object.")
            for field, setting in val.items():
                if field in BOOLEAN_CONFIG_FIELDS:
                    validate_boolean(field, setting)
                elif field in ENUM_CONFIG_FIELDS:
                    validate_enum(field, setting, ENUM_CONFIG_FIELDS[field])
                else:
                    raise ValidationError(
                        f"Unsupported configuration setting: {field}."
                    )
        else:
            raise ValidationError(f"Unsupported preset field: {key}.")


def resolve_config(client, room):
    from django.conf import settings
    from .models import MeetingConfigPreset

    result = {"meetingType": settings.DEFAULT_PROVIDER_MEETING_TYPE}
    default = MeetingConfigPreset.objects.filter(is_system_default=True).first()
    for layer in (
        default.payload if default else {},
        client.default_preset.payload if client.default_preset_id else {},
        room.default_meeting_config or {},
    ):
        validate_config(layer)
        flags = {**result.get("config", {}), **layer.get("config", {})}
        result.update(layer)
        if flags:
            result["config"] = flags
    validate_config(result)
    return result
