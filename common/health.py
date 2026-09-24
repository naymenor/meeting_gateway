from django.db import connection
from django.conf import settings
from redis import Redis
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


class HealthDataSerializer(serializers.Serializer):
    status = serializers.CharField()


class HealthResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = HealthDataSerializer()


class HealthErrorSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    code = serializers.CharField()
    message = serializers.CharField()
    details = serializers.JSONField()
    requestId = serializers.CharField()


@extend_schema(responses={200: HealthResponseSerializer}, auth=[])
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def live(request):
    return Response({"success": True, "data": {"status": "alive"}})


@extend_schema(
    responses={
        200: HealthResponseSerializer,
        503: OpenApiResponse(HealthErrorSerializer, "PostgreSQL or Redis unavailable."),
    },
    auth=[],
)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        Redis.from_url(
            settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2
        ).ping()
    except Exception:
        return Response(
            {
                "success": False,
                "code": "NOT_READY",
                "message": "Dependency unavailable.",
                "details": {},
                "requestId": getattr(request, "request_id", ""),
            },
            status=503,
        )
    return Response({"success": True, "data": {"status": "ready"}})
