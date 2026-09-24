import logging
import time
import uuid
from contextvars import ContextVar
from django.http import JsonResponse

request_context = ContextVar("request_context", default={})
logger = logging.getLogger("gateway.http")


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = str(uuid.uuid4())
        started = time.monotonic()
        token = request_context.set(
            {"request_id": request.request_id, "ip": request.META.get("REMOTE_ADDR")}
        )
        try:
            response = self.get_response(request)
            if (
                response.status_code >= 400
                and request.path.startswith("/api/")
                and not hasattr(response, "data")
            ):
                response = JsonResponse(
                    {
                        "success": False,
                        "code": "NOT_FOUND"
                        if response.status_code == 404
                        else "REQUEST_ERROR",
                        "message": "Request could not be processed.",
                        "details": {},
                        "requestId": request.request_id,
                    },
                    status=response.status_code,
                )
            response["X-Request-ID"] = request.request_id
            response["Cache-Control"] = "no-store"
            logger.info(
                {
                    "event": "http.request_completed",
                    "method": request.method,
                    "route": getattr(
                        getattr(request, "resolver_match", None), "route", "[unmatched]"
                    ),
                    "status_code": response.status_code,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                },
                extra={"request": request, "request_id": request.request_id},
            )
            return response
        finally:
            request_context.reset(token)
