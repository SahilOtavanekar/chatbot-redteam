"""Turn an HTTP response into reply text, or raise a `SendError`.

A non-2xx response is never reply text: an error page scored as a "safe" reply would be a false
Pass. Error details describe what went wrong but never quote the body, which may hold real data.
"""

import json
import math
import re
from collections.abc import Mapping

from jsonpath_ng import parse as parse_jsonpath

from suite.models import SendError

_RETRY_AFTER_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds from a numeric `Retry-After`; None for a missing, HTTP-date or odd value."""
    for name, value in headers.items():
        if name.lower() != "retry-after":
            continue
        text = value.strip()
        if not _RETRY_AFTER_SECONDS.fullmatch(text):
            return None
        seconds = float(text)
        return seconds if math.isfinite(seconds) else None
    return None


def extract_reply(*, status: int, headers: Mapping[str, str], body: bytes, reply_path: str) -> str:
    """Return the reply text at `reply_path` in a JSON response body, unchanged.

    Raises `SendError` for a non-2xx status (`http_status`, with `retry_after` when the server
    sent a numeric `Retry-After`), a body that is not UTF-8 JSON, a path with no match, a
    non-string value (`unreadable_reply`), or a blank string (`empty_reply`).
    """
    if not 200 <= status <= 299:
        raise SendError(
            "http_status", f"HTTP {status}", status=status, retry_after=_retry_after(headers)
        )
    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):  # bad UTF-8, bad JSON, or nesting too deep to parse
        raise SendError("unreadable_reply", "response is not JSON") from None
    matches = parse_jsonpath(reply_path).find(document)
    if not matches:
        raise SendError("unreadable_reply", f"no value at {reply_path}")
    value = matches[0].value
    if not isinstance(value, str):
        raise SendError(
            "unreadable_reply", f"value at {reply_path} is not a string ({type(value).__name__})"
        )
    if not value.strip():
        raise SendError("empty_reply", "reply is empty")
    return value
