from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.rooms.configuration import (
    BOOLEAN_CONFIG_FIELDS,
    BOOLEAN_TOP_LEVEL_FIELDS,
    validate_config,
)
from apps.rooms.models import MeetingConfigPreset


@pytest.mark.parametrize(
    "field,allowed",
    [
        ("PRTCPNTS_LIST", ("host", "all")),
        ("REJOIN_POPUP", ("host", "all", "none")),
        ("SHR_OVERLAY", ("host", "all", "none")),
    ],
)
def test_config_enums(field, allowed):
    for value in allowed:
        payload = {"config": {field: value}}
        before = deepcopy(payload)
        # Exercise the actual model field validator used by Django Admin.
        MeetingConfigPreset._meta.get_field("payload").clean(payload, None)
        assert payload == before

    for value in (True, False, "invalid", "HOST", "", None, 1, [], {}):
        with pytest.raises(ValidationError) as error:
            validate_config({"config": {field: value}})
        assert error.value.messages == [
            f"Invalid value for {field}. Allowed values: {', '.join(allowed)}."
        ]


@pytest.mark.parametrize("field", sorted(BOOLEAN_CONFIG_FIELDS))
def test_boolean_config_fields(field):
    for value in (True, False):
        validate_config({"config": {field: value}})
    for value in ("true", "host", 0, 1, None, ["BD"]):
        with pytest.raises(ValidationError, match=f"Invalid value for {field}"):
            validate_config({"config": {field: value}})


@pytest.mark.parametrize("field", sorted(BOOLEAN_TOP_LEVEL_FIELDS))
def test_boolean_top_level_fields(field):
    for value in (True, False):
        validate_config({field: value})
    for value in ("true", 0, 1, None):
        with pytest.raises(ValidationError, match=f"Invalid value for {field}"):
            validate_config({field: value})


def test_meeting_type():
    for value in ("instant", "scheduled"):
        validate_config({"meetingType": value})
    for value in (True, "invalid", None):
        with pytest.raises(ValidationError, match="Allowed values: instant, scheduled"):
            validate_config({"meetingType": value})


@pytest.mark.parametrize(
    "payload",
    [
        {"unsupported": True},
        {"config": {"unsupported": True}},
        {"PRTCPNTS_LIST": "host"},
        {"config": {"meetingType": "instant"}},
        {"config": {"bigMeeting": True}},
        {"config": []},
        [],
    ],
)
def test_unknown_fields_and_wrong_containers_rejected(payload):
    with pytest.raises(ValidationError):
        validate_config(payload)
