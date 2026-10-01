from django.conf import settings
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import audit
from apps.meetings.serializers import ErrorResponseSerializer
from .models import IntegrationClient
from .tokens import (
    authenticate_credentials,
    issue_token,
    throttle_exchange,
    MachineAuthenticationError,
)


class TokenRequestSerializer(serializers.Serializer):
    client_id = serializers.CharField(max_length=36)
    client_secret = serializers.CharField(
        max_length=256, trim_whitespace=False, write_only=True
    )


class TokenDataSerializer(serializers.Serializer):
    accessToken = serializers.CharField()
    tokenType = serializers.CharField()
    expiresIn = serializers.IntegerField()
    scopes = serializers.ListField(child=serializers.CharField())


class TokenResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    data = TokenDataSerializer()


@method_decorator(sensitive_post_parameters("client_secret"), name="dispatch")
class TokenView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_authenticate_header(self, request):
        return 'Bearer realm="Meeting Gateway"'

    @extend_schema(
        operation_id="create_gateway_token",
        auth=[],
        request=TokenRequestSerializer,
        responses={
            200: TokenResponseSerializer,
            401: OpenApiResponse(ErrorResponseSerializer, "INVALID_CLIENT."),
            429: OpenApiResponse(ErrorResponseSerializer, "Token exchange rate limited."),
        },
        description=(
            "POST /api/v1/auth/token/ exchanges client credentials for a Gateway Access Token "
            "used by LMS to authenticate to Meeting Gateway. Default lifetime is 3600 seconds / "
            "60 minutes, configured by GATEWAY_ACCESS_TOKEN_TTL_SECONDS; expiresIn reflects runtime settings. "
            "Cache it server-side and obtain another token using client_id/client_secret near expiry "
            "or after 401 TOKEN_EXPIRED, then retry the original request. No refresh-token mechanism. "
            "The separate Convay Access Token is returned by successful meeting operations or "
            "convay-token; its provider-controlled lifetime is observed around 6 hours. Use returned "
            "expiresAt rather than assuming a duration. Convay tokens are server-to-server only; "
            "the Gateway reauthenticates automatically."
        ),
        examples=[
            OpenApiExample(
                "Client credentials",
                value={
                    "client_id": "example-client",
                    "client_secret": "example-secret",
                },
                request_only=True,
            ),
            OpenApiExample(
                "Gateway access token",
                value={
                    "success": True,
                    "data": {
                        "accessToken": "FAKE_GATEWAY_JWT",
                        "tokenType": "Bearer",
                        "expiresIn": 3600,
                        "scopes": [
                            "meeting:write",
                            "meeting:read",
                            "meeting:token",
                            "room:read",
                        ],
                    },
                },
                response_only=True,
                status_codes=["200"],
            ),
        ],
    )
    def post(self, request):
        remote = request.META.get("REMOTE_ADDR", "")
        throttle_exchange(remote)
        serializer = TokenRequestSerializer(data=request.data)
        if not serializer.is_valid():
            raise MachineAuthenticationError("INVALID_CLIENT")
        client = authenticate_credentials(remote=remote, **serializer.validated_data)
        token = issue_token(client)
        IntegrationClient.objects.filter(pk=client.pk).update(
            last_used_at=timezone.now()
        )
        audit("gateway_token_issued", client, client=client)
        return Response(
            {
                "success": True,
                "data": {
                    "accessToken": token,
                    "tokenType": "Bearer",
                    "expiresIn": settings.GATEWAY_ACCESS_TOKEN_TTL_SECONDS,
                    "scopes": sorted(set(client.scopes)),
                },
            }
        )
