from datetime import datetime
from django.utils.dateparse import parse_datetime
from rest_framework import serializers


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise serializers.ValidationError("Unknown fields are not permitted.")
        return super().to_internal_value(data)


class AwareDateTimeField(serializers.DateTimeField):
    def to_internal_value(self, value):
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else value
            if (
                not isinstance(parsed, datetime)
                or parsed.tzinfo is None
                or parsed.utcoffset() is None
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise serializers.ValidationError("Use timezone-aware ISO-8601.")
        return super().to_internal_value(value)


class ReferenceSerializer(StrictSerializer):
    id = serializers.CharField(max_length=160)
    name = serializers.CharField(max_length=200)


class ClassSerializer(StrictSerializer):
    id = serializers.CharField(max_length=160)
    date = serializers.DateField()


class RegistrationSerializer(StrictSerializer):
    meetingTitle = serializers.CharField(max_length=250)
    teacher = ReferenceSerializer()
    subject = ReferenceSerializer(required=False)
    batch = ReferenceSerializer()
    class_info = ClassSerializer(source="class")
    scheduleType = serializers.ChoiceField(
        choices=["SCHEDULED", "INSTANT"], default="SCHEDULED"
    )

    def get_fields(self):
        fields = super().get_fields()
        field = fields.pop("class_info")
        field.source = None
        fields["class"] = field
        return fields


class BookingSerializer(StrictSerializer):
    roomId = serializers.CharField(max_length=64, required=False)
    startAt = AwareDateTimeField()
    endAt = AwareDateTimeField()

    def validate(self, attrs):
        if attrs["endAt"] <= attrs["startAt"]:
            raise serializers.ValidationError("endAt must be after startAt.")
        return attrs


class AvailabilitySerializer(StrictSerializer):
    date = serializers.DateField()
    start_at = AwareDateTimeField(required=False)
    end_at = AwareDateTimeField(required=False)

    def validate(self, attrs):
        if ("start_at" in attrs) != ("end_at" in attrs):
            raise serializers.ValidationError("Provide both interval endpoints.")
        if "start_at" in attrs and attrs["end_at"] <= attrs["start_at"]:
            raise serializers.ValidationError("End must be after start.")
        return attrs


class SearchSerializer(StrictSerializer):
    id = serializers.UUIDField(required=False)
    external_class_id = serializers.CharField(required=False)
    teacher_id = serializers.CharField(required=False)
    teacher_name = serializers.CharField(required=False)
    batch_id = serializers.CharField(required=False)
    subject_id = serializers.CharField(required=False)
    subject_name = serializers.CharField(required=False)
    room_id = serializers.CharField(required=False)
    provider_calendar_id = serializers.CharField(required=False)
    status = serializers.ChoiceField(
        choices=[
            "DRAFT",
            "RESERVED",
            "PROVISIONING",
            "READY",
            "LIVE",
            "ENDED",
            "CANCELLED",
            "FAILED",
            "PROVIDER_RESPONSE_INVALID",
            "PROVIDER_STATE_UNKNOWN",
        ],
        required=False,
    )
    class_date = serializers.DateField(required=False)
    start_from = AwareDateTimeField(required=False)
    start_to = AwareDateTimeField(required=False)
    created_from = AwareDateTimeField(required=False)
    created_to = AwareDateTimeField(required=False)
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=25)


class EnvelopeSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = serializers.JSONField(required=False)
    code = serializers.CharField(required=False)
    message = serializers.CharField(required=False)
    details = serializers.JSONField(required=False)
    requestId = serializers.CharField(required=False)


class ClassInfoSerializer(serializers.Serializer):
    classId = serializers.CharField()
    teacher = ReferenceSerializer()
    subject = ReferenceSerializer()
    batch = ReferenceSerializer()


class MeetingInfoSerializer(serializers.Serializer):
    meetingTitle = serializers.CharField()
    classDate = serializers.DateField()
    scheduleType = serializers.ChoiceField(choices=["scheduled", "instant"])
    startAt = serializers.DateTimeField(allow_null=True)
    endAt = serializers.DateTimeField(allow_null=True)
    status = serializers.ChoiceField(
        choices=[
            "DRAFT",
            "RESERVED",
            "PROVISIONING",
            "READY",
            "LIVE",
            "ENDED",
            "CANCELLED",
            "FAILED",
            "PROVIDER_RESPONSE_INVALID",
            "PROVIDER_STATE_UNKNOWN",
        ]
    )


class RoomInfoSerializer(serializers.Serializer):
    roomId = serializers.CharField()
    roomName = serializers.CharField()


class ProviderAuthorizationSerializer(serializers.Serializer):
    accessToken = serializers.CharField()
    tokenType = serializers.CharField()
    expiresAt = serializers.DateTimeField(allow_null=True)


class ConvayInfoSerializer(serializers.Serializer):
    meetingType = serializers.CharField()
    calendarId = serializers.CharField(allow_null=True)
    meetingPanelAddress = serializers.CharField(allow_null=True)
    startMeetingUrl = serializers.URLField(required=False, allow_null=True)
    authorization = ProviderAuthorizationSerializer(required=False)


class MeetingDataSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    classInfo = ClassInfoSerializer()
    meetingInfo = MeetingInfoSerializer()
    roomInfo = RoomInfoSerializer(allow_null=True)
    convay = ConvayInfoSerializer()
    createdAt = serializers.DateTimeField()


class BookedSlotSerializer(serializers.Serializer):
    startAt = serializers.DateTimeField()
    endAt = serializers.DateTimeField()
    status = serializers.ChoiceField(choices=["BOOKED"])


class RoomAvailabilitySerializer(serializers.Serializer):
    roomId = serializers.CharField()
    roomName = serializers.CharField()
    bookedSlots = BookedSlotSerializer(many=True)
    available = serializers.BooleanField(required=False)


class RegistrationDataSerializer(MeetingDataSerializer):
    rooms = RoomAvailabilitySerializer(many=True)


class MeetingResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = MeetingDataSerializer()


class RegistrationResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = RegistrationDataSerializer()


class SearchDataSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    page = serializers.IntegerField()
    pageSize = serializers.IntegerField()
    results = MeetingDataSerializer(many=True)


class SearchResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = SearchDataSerializer()


class AvailabilityDataSerializer(serializers.Serializer):
    date = serializers.DateField()
    rooms = RoomAvailabilitySerializer(many=True)


class AvailabilityResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = AvailabilityDataSerializer()


class ConvayTokenInfoSerializer(serializers.Serializer):
    calendarId = serializers.CharField(allow_null=True)
    authorization = ProviderAuthorizationSerializer()


class ConvayTokenDataSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    roomInfo = RoomInfoSerializer()
    convay = ConvayTokenInfoSerializer()


class ConvayTokenResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = ConvayTokenDataSerializer()


class ErrorResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    code = serializers.CharField()
    message = serializers.CharField()
    details = serializers.JSONField()
    requestId = serializers.CharField()
