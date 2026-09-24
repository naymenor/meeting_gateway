from rest_framework.authentication import BaseAuthentication, get_authorization_header
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from .tokens import validate_token, MachineAuthenticationError


class ClientAuthentication(BaseAuthentication):
    def authenticate_header(self, request):
        return 'Bearer realm="Meeting Gateway"'

    def authenticate(self, request):
        header = get_authorization_header(request).split()
        if len(header) != 2 or header[0].lower() != b"bearer" or len(header[1]) > 16384:
            raise MachineAuthenticationError("INVALID_TOKEN")
        try:
            token = header[1].decode("ascii")
        except UnicodeError:
            raise MachineAuthenticationError("INVALID_TOKEN") from None
        return validate_token(token, request.META.get("REMOTE_ADDR", ""))


class ClientScheme(OpenApiAuthenticationExtension):
    target_class = "apps.integrations.authentication.ClientAuthentication"
    name = "GatewayBearer"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Obtain a Gateway accessToken from POST /api/v1/auth/token/. Paste the token here. Do not use a Convay JWT.",
        }
