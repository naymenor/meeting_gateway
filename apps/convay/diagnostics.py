"""Bounded diagnostic extraction; provider bodies must never be logged directly."""

import json
import re
from urllib.parse import urlsplit

from common.logging import SENSITIVE

REDACTED = "[REDACTED]"
DIAGNOSTIC_FIELDS = {
    "message",
    "error",
    "errors",
    "detail",
    "details",
    "code",
    "status",
    "statuscode",
    "success",
    "title",
    "type",
    "validationerrors",
    "fielderrors",
}

OMITTED_DIAGNOSTIC_FIELDS = {"stacktrace", "trace", "exceptiontrace"}


def sensitive_key(key):
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(word in normalized for word in SENSITIVE) or normalized in {
        "username",
        "email",
        "encryptionkey",
        "apikey",
    }


def sensitive_values(value, depth=0):
    if depth > 8:
        return []
    values = []
    if isinstance(value, dict):
        for key, item in value.items():
            if sensitive_key(key) and isinstance(item, str) and item:
                values.append(item)
            values.extend(sensitive_values(item, depth + 1))
    elif isinstance(value, list):
        for item in value:
            values.extend(sensitive_values(item, depth + 1))
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            values.extend(sensitive_values(json.loads(value), depth + 1))
        except (ValueError, TypeError, RecursionError):
            pass
    return values


def sanitize_text(value, secrets=()):
    for secret in sorted(
        set(s for s in secrets if isinstance(s, str) and s), key=len, reverse=True
    ):
        value = value.replace(secret, REDACTED)
    value = re.sub(r'https?://[^\s<>"\']+', "[REDACTED_URL]", value, flags=re.I)
    value = re.sub(r'\bBearer\s+[^\s,"\']+', "Bearer [REDACTED]", value, flags=re.I)
    value = re.sub(
        r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\b", REDACTED, value
    )
    value = re.sub(
        r'(?i)\b(password|authorization|(?:access[_-]?|refresh[_-]?|client[_-]?)?token|(?:client[_-]?)?secret|encryption[_-]?key)\s*[:=]\s*(?:"[^"]*"|\'[^\']*\'|[^\s,;]+)',
        r"\1=[REDACTED]",
        value,
    )
    return value[:512]


def sanitize_payload(value, secrets=(), depth=0):
    """Preserve outbound JSON while removing credential-bearing values."""
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            # Convay's PASSWORD config flag is a Boolean, not a credential.
            boolean_password_flag = normalized == "password" and type(item) is bool
            if sensitive_key(key) and not boolean_password_flag:
                result[str(key)] = REDACTED
            else:
                result[str(key)] = sanitize_payload(item, secrets, depth + 1)
        return result
    if isinstance(value, list):
        return [sanitize_payload(item, secrets, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return sanitize_text(value, secrets)
    if value is None or type(value) in (bool, int, float):
        return value
    return "[OMITTED]"


def sanitize_response(response, secrets=(), preserve_safe_fields=False):
    if response is None:
        return None
    # Permit moderately large JSON error envelopes so removable stack traces do
    # not hide useful status/message/fieldErrors. Keep a hard parsing bound.
    if len(response.content) > 1048576:
        return {"summary": "Upstream body omitted: exceeds diagnostic size limit."}
    try:
        body = response.json()
    except (ValueError, TypeError, RecursionError):
        if len(response.content) > 65536:
            return {"summary": "Upstream body omitted: exceeds diagnostic size limit."}
        return {
            "text": sanitize_text(response.text, secrets),
            "content_type": response.headers.get("content-type", "").split(";", 1)[0],
        }
    secrets = tuple(secrets) + tuple(sensitive_values(body))

    def walk(value, depth=0, diagnostic=False):
        if depth > 6:
            return "[TRUNCATED]"
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:40]:
                safe_key = sanitize_text(str(key), secrets)
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in OMITTED_DIAGNOSTIC_FIELDS:
                    continue
                if sensitive_key(key):
                    result[safe_key] = REDACTED
                elif diagnostic or normalized in DIAGNOSTIC_FIELDS:
                    result[safe_key] = walk(item, depth + 1, True)
                elif isinstance(item, (dict, list)) or (
                    isinstance(item, str) and item.lstrip().startswith(("{", "["))
                ):
                    result[safe_key] = walk(item, depth + 1)
                else:
                    result[safe_key] = "[OMITTED]"
            return result
        if isinstance(value, list):
            return [walk(item, depth + 1, diagnostic) for item in value[:20]]
        if isinstance(value, str):
            # Some provider envelopes embed JSON objects as strings.
            try:
                nested = json.loads(value)
                if isinstance(nested, (dict, list)):
                    return walk(nested, depth + 1, diagnostic)
            except (ValueError, TypeError, RecursionError):
                pass
            return sanitize_text(value, secrets) if diagnostic else "[OMITTED]"
        return value if diagnostic else "[OMITTED]"

    result = walk(body, diagnostic=preserve_safe_fields)
    encoded = json.dumps(result)
    return (
        result
        if len(encoded) <= 8192
        else {"summary": "Truncated sanitized diagnostics", "preview": encoded[:8192]}
    )


def endpoint_path(path):
    return sanitize_text(urlsplit(path).path)
