import logging
from common.logging import exception_diagnostics
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_handler
from rest_framework.response import Response


class GatewayError(APIException):
    def __init__(self, code, message, status=400):
        self.status_code = status
        self.default_code = code
        super().__init__(message, code)


def exception_handler(exc, context):
    response = drf_handler(exc, context)
    request_id = getattr(context.get("request"), "request_id", "")
    if response is None:
        request = context.get("request")
        logging.getLogger("gateway.errors").error(
            {
                "event": "http.unexpected_exception",
                "method": getattr(request, "method", None),
                "route": getattr(getattr(request, "resolver_match", None), "route", "[unmatched]"),
                **exception_diagnostics(exc, request),
            },
            extra={"request_id": request_id},
        )
        return Response(
            {
                "success": False,
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred.",
                "details": {},
                "requestId": request_id,
            },
            status=500,
        )
    response.data = {
        "success": False,
        "code": getattr(exc, "default_code", "REQUEST_ERROR").upper(),
        "message": str(exc.detail)
        if isinstance(exc, GatewayError)
        else "Request could not be processed.",
        "details": response.data if response.status_code == 400 else {},
        "requestId": request_id,
    }
    return response
