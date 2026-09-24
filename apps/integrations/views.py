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
        auth=[],
        request=TokenRequestSerializer,
        responses={
            200: TokenResponseSerializer,
            401: OpenApiResponse(ErrorResponseSerializer, "INVALID_CLIENT."),
            429: OpenApiResponse(ErrorResponseSerializer, "Token exchange rate limited."),
        },
        description="Exchange machine client credentials for a short-lived Gateway JWT. No Django session or Admin login required. This is not a Convay token.",
        examples=[
            OpenApiExample(
                "Client credentials",
                value={
                    "client_id": "11111111-1111-4111-8111-111111111111",
                    "client_secret": "FAKE_CLIENT_SECRET",
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
                            "meeting:cancel",
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
