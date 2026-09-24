import json
import logging
from common.middleware import request_context

SENSITIVE = (
    "password",
    "secret",
    "token",
    "authorization",
    "startmeetingurl",
    "credential",
)


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if any(s in k.lower().replace("_", "") for s in SENSITIVE)
            and not (k.lower().replace("_", "") == "password" and type(v) is bool)
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class SafeJSONFormatter(logging.Formatter):
    def format(self, record):
        request = getattr(record, "request", None)
        request_id = (
            getattr(record, "request_id", None)
            or getattr(request, "request_id", None)
            or request_context.get().get("request_id")
        )
        # Never interpolate arbitrary messages, request bodies, headers or exceptions.
        if isinstance(record.msg, dict):
            event = redact(record.msg)
        elif record.name == "django.request":
            event = {
                "event": "http.request_failed",
                "status_code": getattr(record, "status_code", None),
                "method": getattr(request, "method", None),
                "route": getattr(
                    getattr(request, "resolver_match", None), "route", "[unmatched]"
                ),
            }
        else:
            event = {"event": "application_event"}
        return json.dumps(
            {
                **event,
                "level": record.levelname,
                "logger": record.name,
                "timestamp": record.created,
                "request_id": request_id,
                "requestId": request_id,
            }
        )
