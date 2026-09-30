from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    OpenApiExample,
    OpenApiResponse,
)
from apps.audit.services import audit
from apps.rooms.services import availability
from apps.convay.client import ProviderError
from apps.convay.tokens import get_token
from common.exceptions import GatewayError
from .models import Meeting
from .serializers import (
    CreateMeetingSerializer,
    AvailabilitySerializer,
    SearchSerializer,
    ErrorResponseSerializer,
    MeetingResponseSerializer,
    SearchResponseSerializer,
    AvailabilityResponseSerializer,
    ConvayTokenResponseSerializer,
)
from .services import register, provision, representation, authorization
ERROR_RESPONSES = {
    400: OpenApiResponse(ErrorResponseSerializer, "Invalid request."),
    401: OpenApiResponse(
        ErrorResponseSerializer, "INVALID_TOKEN or TOKEN_EXPIRED."
    ),
    403: OpenApiResponse(ErrorResponseSerializer, "INSUFFICIENT_SCOPE."),
    404: OpenApiResponse(ErrorResponseSerializer, "Meeting not found for this client."),
    409: OpenApiResponse(
        ErrorResponseSerializer,
        "State, room slot, capacity, or creation-in-progress conflict.",
    ),
    422: OpenApiResponse(
        ErrorResponseSerializer, "Provider contract or validation rejection."
    ),
    502: OpenApiResponse(
        ErrorResponseSerializer,
        "Provider authentication, authorization, endpoint, response, or unknown-state failure.",
    ),
    503: OpenApiResponse(
        ErrorResponseSerializer, "Provider unavailable or rate limited."
    ),
}

REGISTRATION_REQUEST = {
    "meetingTitle": "API Test Physics",
    "teacher": {"id": "T-001", "name": "Test Teacher"},
    "batch": {"id": "B-001", "name": "Test Batch"},
    "class": {"id": "CLS-001", "date": "2026-09-24"},
}
MEETING_DATA = {
    "id": "11111111-1111-4111-8111-111111111111",
    "classInfo": {
        "classId": "CLS-001",
        "teacher": {"id": "T-001", "name": "Test Teacher"},
        "batch": {"id": "B-001", "name": "Test Batch"},
    },
    "meetingInfo": {
        "meetingTitle": "API Test Physics",
        "classDate": "2026-09-24",
        "scheduleType": "scheduled",
        "startAt": None,
        "endAt": None,
        "status": "DRAFT",
    },
    "convay": {
        "meetingType": "instant",
        "calendarId": None,
        "meetingPanelAddress": None,
    },
    "createdAt": "2026-09-24T10:00:00+06:00",
}
CREATE_REQUEST = {
    "startAt": "2026-09-24T17:00:00+06:00",
    "endAt": "2026-09-24T18:00:00+06:00",
}


def require_scope(request, *scopes):
    if any(s not in request.user.scopes for s in scopes):
        raise GatewayError("INSUFFICIENT_SCOPE", "Required scope is missing.", 403)


def owned(request, pk):
    return get_object_or_404(
        Meeting.objects.select_related("room", "integration_client"),
        pk=pk,
        integration_client=request.user,
    )


class MeetingListView(APIView):
    @extend_schema(
        operation_id="create_meeting",
        request=CreateMeetingSerializer,
        responses={
            200: OpenApiResponse(
                MeetingResponseSerializer,
                "Meeting was already READY; existing result returned.",
            ),
            201: MeetingResponseSerializer,
            **ERROR_RESPONSES,
        },
        description=(
            "Requires meeting:write. Room/license allocation is handled automatically by the Meeting Gateway. "
            "Obtain a Gateway Bearer token, check availability, "
            "choose a time, then POST metadata and startAt/endAt here. "
            "Availability is only a snapshot: Gateway atomically rechecks and reserves the "
            "capacity, authenticates internally and creates Convay before returning READY (201). "
            "A duplicate client + class.id in READY/LIVE returns the existing meeting (200). "
            "RESERVED/PROVISIONING returns 409 CREATION_IN_PROGRESS; unresolved provider "
            "outcomes return 409 PROVIDER_RECONCILIATION_REQUIRED. Exhausted capacity returns "
            "409 NO_CAPACITY_AVAILABLE; refresh availability and select another time. "
            "Timestamps must be timezone-aware and are stored in UTC. "
            "Authorization and startMeetingUrl require meeting:token. Use convay-token "
            "later for current authorization. ENDED returns 409 CLASS_ALREADY_COMPLETED; "
            "FAILED returns 409 PREVIOUS_CREATION_FAILED; CANCELLED returns 409 CLASS_CANCELLED. "
            "Unsupported legacy states or duplicate logical rows return 409 "
            "EXISTING_MEETING_REQUIRES_REVIEW. These states never trigger provider creation."
        ),
        examples=[
            OpenApiExample(
                "Meeting creation", value={**REGISTRATION_REQUEST, **CREATE_REQUEST}, request_only=True
            ),
            OpenApiExample(
                "Successful meeting creation",
                value={
                    "success": True,
                    "data": {
                        **MEETING_DATA,
                        "meetingInfo": {
                            **MEETING_DATA["meetingInfo"],
                            "startAt": CREATE_REQUEST["startAt"],
                            "endAt": CREATE_REQUEST["endAt"],
                            "status": "READY",
                        },
                        "convay": {
                            "meetingType": "instant",
                            "calendarId": "fake-calendar-id",
                            "meetingPanelAddress": "meet.convay.com",
                            "startMeetingUrl": "https://meet.convay.com/example?jwt=FAKE",
                            "authorization": {
                                "accessToken": "FAKE_CONVAY_ACCESS_TOKEN",
                                "tokenType": "Bearer",
                                "expiresAt": None,
                            },
                        },
                    },
                },
                response_only=True,
                status_codes=["200", "201"],
            ),
            OpenApiExample(
                "No capacity",
                value={
                    "success": False,
                    "code": "NO_CAPACITY_AVAILABLE",
                    "message": "No meeting capacity is available for the requested time.",
                    "details": {},
                    "requestId": "22222222-2222-4222-8222-222222222222",
                },
                response_only=True,
                status_codes=["409"],
            ),
            OpenApiExample(
                "Provider validation rejection",
                value={
                    "success": False,
                    "code": "PROVIDER_VALIDATION_ERROR",
                    "message": "Convay rejected the meeting payload.",
                    "details": {},
                    "requestId": "22222222-2222-4222-8222-222222222222",
                },
                response_only=True,
                status_codes=["422"],
            ),
            OpenApiExample(
                "Provider creation outcome unknown",
                value={
                    "success": False,
                    "code": "PROVIDER_STATE_UNKNOWN",
                    "message": "Provider creation outcome is unknown; reservation retained.",
                    "details": {},
                    "requestId": "22222222-2222-4222-8222-222222222222",
                },
                response_only=True,
                status_codes=["502"],
            ),
        ],
    )
    def post(self, request):
        require_scope(request, "meeting:write")
        serializer = CreateMeetingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        meeting, _ = register(request.user, values)
        current, token = provision(meeting, values)
        created = token is not None
        if "meeting:token" in request.user.scopes:
            if token is None:
                token = get_token(current.room, meeting_id=current.pk)
            audit("convay_token_issued", current, client=request.user)
        else:
            token = None
        return Response(
            {"success": True, "data": representation(current, token)},
            status=201 if created else 200,
        )


    @extend_schema(
        operation_id="list_meetings",
        parameters=[SearchSerializer],
        responses={200: SearchResponseSerializer, **ERROR_RESPONSES},
        description="Search only the caller’s meetings. Requires meeting:read. No bearer tokens or sensitive URLs returned.",
    )
    def get(self, request):
        require_scope(request, "meeting:read")
        serializer = SearchSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data.copy()
        page, size = values.pop("page"), values.pop("page_size")
        mapping = {
            "teacher_name": "teacher_name__icontains",
            "start_from": "start_at__gte",
            "start_to": "start_at__lte",
            "created_from": "created_at__gte",
            "created_to": "created_at__lte",
        }
        query = (
            Meeting.objects.filter(
                integration_client=request.user,
                **{mapping.get(k, k): v for k, v in values.items()},
            )
            .select_related("room")
            .order_by("-created_at", "-id")
        )
        return Response(
            {
                "success": True,
                "data": {
                    "count": query.count(),
                    "page": page,
                    "pageSize": size,
                    "results": [
                        representation(m)
                        for m in query[(page - 1) * size : page * size]
                    ],
                },
            }
        )


class MeetingDetailView(APIView):
    @extend_schema(
        operation_id="retrieve_meeting",
        responses={200: MeetingResponseSerializer, **ERROR_RESPONSES},
        description="Requires meeting:read.",
    )
    def get(self, request, pk):
        require_scope(request, "meeting:read")
        return Response({"success": True, "data": representation(owned(request, pk))})


class MeetingTokenView(APIView):
    @extend_schema(
        operation_id="get_convay_token",
        request=None,
        responses={200: ConvayTokenResponseSerializer, **ERROR_RESPONSES},
        description="Requires meeting:token. Uses Gateway UUID, never calendarId. Returns account-scoped access token, never refresh token.",
        examples=[
            OpenApiExample(
                "Regenerated Convay authorization",
                value={
                    "success": True,
                    "data": {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "convay": {
                            "calendarId": "fake-calendar-id",
                            "authorization": {
                                "accessToken": "FAKE_CONVAY_ACCESS_TOKEN",
                                "tokenType": "Bearer",
                                "expiresAt": None,
                            },
                        },
                    },
                },
                response_only=True,
                status_codes=["200"],
            )
        ],
    )
    def post(self, request, pk):
        require_scope(request, "meeting:token")
        meeting = owned(request, pk)
        if (
            meeting.status not in (Meeting.Status.READY, Meeting.Status.LIVE)
            or not meeting.room_id
        ):
            raise GatewayError(
                "INVALID_STATE",
                "Tokens are available only for ready or live meetings.",
                409,
            )
        try:
            token = get_token(meeting.room, meeting_id=meeting.pk)
        except ProviderError as exc:
            audit("provider_error", meeting, client=request.user, provider_error=exc)
            status = (
                503
                if exc.code in ("PROVIDER_RATE_LIMITED", "PROVIDER_UNAVAILABLE")
                else 502
            )
            raise GatewayError(
                exc.code, "Provider authentication failed.", status
            ) from None
        audit("convay_token_issued", meeting, client=request.user)
        return Response(
            {
                "success": True,
                "data": {
                    "id": str(meeting.pk),
                    "convay": {
                        "calendarId": meeting.provider_calendar_id,
                        "authorization": authorization(token),
                    },
                },
            }
        )


class AvailabilityView(APIView):
    @extend_schema(
        operation_id="check_meeting_availability",
        parameters=[AvailabilitySerializer],
        responses={200: AvailabilityResponseSerializer, **ERROR_RESPONSES},
        description="Requires room:read and timezone-aware start_at/end_at with end_at > start_at. Room/license allocation is handled automatically by the Meeting Gateway. Availability is a point-in-time snapshot and is atomically revalidated when the meeting is created. Checks buffers, internal configuration and provider limits. Unavailable capacity returns 200 with available=false.",
        examples=[
            OpenApiExample(
                "Meeting availability",
                value={
                    "success": True,
                    "data": {"startAt": "2026-09-30T10:00:00Z", "endAt": "2026-09-30T11:00:00Z", "available": True},
                },
                response_only=True,
                status_codes=["200"],
            )
        ],
    )
    def get(self, request):
        require_scope(request, "room:read")
        serializer = AvailabilitySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        return Response(
            {
                "success": True,
                "data": availability(request.user, values["start_at"], values["end_at"]),
            }
        )
