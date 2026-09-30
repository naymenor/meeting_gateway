import json
import logging
from pathlib import Path
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


def exception_diagnostics(exc, request=None):
    """Sanitized messages and source-free stack frames, never local/source dumps."""
    from django.conf import settings
    from apps.convay.diagnostics import sanitize_text, sensitive_values

    secrets = []
    for name in (
        "SECRET_KEY", "GATEWAY_JWT_SIGNING_KEY", "CREDENTIAL_ENCRYPTION_KEY",
        "CREDENTIAL_ENCRYPTION_PREVIOUS_KEYS",
    ):
        value = getattr(settings, name, None)
        if isinstance(value, str):
            secrets.append(value)
        elif isinstance(value, dict):
            secrets.extend(value.values())
    # Inspect already parsed data only: logging must not trigger body parsing.
    if request is not None:
        secrets.extend(sensitive_values(getattr(request, "__dict__", {})))
        secrets.extend(sensitive_values(getattr(request, "META", {})))
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        frames = []
        tb = current.__traceback__
        while tb is not None:
            # Values are used solely for scrubbing, never emitted in the log.
            local_values = tb.tb_frame.f_locals
            secrets.extend(sensitive_values(local_values))
            # Auth-result objects carry tokens as attributes rather than dict keys.
            for value in local_values.values():
                attributes = getattr(value, "__dict__", None)
                if isinstance(attributes, dict):
                    secrets.extend(sensitive_values(attributes))
            frames.append({
                "file": Path(tb.tb_frame.f_code.co_filename).name,
                "line": tb.tb_lineno,
                "function": tb.tb_frame.f_code.co_name,
            })
            tb = tb.tb_next
        try:
            message = str(current)
        except Exception:
            message = "[Exception message unavailable]"
        secrets.extend(sensitive_values(message))
        chain.append((type(current).__name__, message, frames[-64:]))
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return {
        "exception_class": type(exc).__name__,
        "exception_message": sanitize_text(chain[0][1], secrets),
        "traceback": [
            {"exception_class": name, "exception_message": sanitize_text(message, secrets),
             "frames": frames}
            for name, message, frames in chain
        ],
    }


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
            if record.exc_info and record.exc_info[1] is not None:
                event.update(exception_diagnostics(record.exc_info[1], request))
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
