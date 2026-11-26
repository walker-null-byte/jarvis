"""Shared LLM error types (re-exported by jarvis.llm) and error explanation."""

import json
import re
from urllib.parse import urlparse

MAX_MESSAGE_CHARS = 500
MAX_DETAIL_CHARS = 6000


class LLMError(RuntimeError):
    """Normalized LLM request failure.

    ``retryable`` is an explicit hint for the facade's retry loop; leave it
    as None to let the loop classify the underlying exception. ``details``
    carries the raw provider text for an expandable "Details" view.
    """

    def __init__(self, message, retryable=None, status=None, details="", provider=""):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.details = details
        self.provider = provider


class Cancelled(Exception):
    """Raised from a streaming callback when the user pressed Stop."""


def _dig_message(body):
    """The human-readable message inside a provider error body, or ''."""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, list):
        for item in body:
            found = _dig_message(item)
            if found:
                return found
        return ""
    if isinstance(body, dict):
        for key in ("error", "errors", "message", "detail", "error_description", "msg"):
            if key in body:
                found = _dig_message(body[key])
                if found:
                    return found
    return ""


def _raw_text(error, status=None):
    """What the server actually said (pretty-printed when JSON); str(error) if nothing else."""
    response = getattr(error, "response", None)
    body = ""
    if response is not None:
        try:
            body = (response.text or "").strip()
        except Exception:
            body = ""
        try:
            body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    if body:
        return (f"HTTP {status}\n" if status else "") + body[:MAX_DETAIL_CHARS]
    return str(error)[:MAX_DETAIL_CHARS]


def _host(base_url):
    try:
        return urlparse(base_url).netloc or base_url
    except Exception:
        return base_url or "the server"


def explain_error(error, provider_label="", base_url=""):
    """Return ``(message, status, details)`` for any exception from a backend."""
    if isinstance(error, LLMError):
        return str(error), error.status, error.details or ""
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if not isinstance(status, int):
        status = None
    body = getattr(error, "body", None)
    message = _dig_message(body)
    if not message:
        response = getattr(error, "response", None)
        if response is not None:
            try:
                message = _dig_message(response.json())
            except Exception:
                message = ""
    names = {cls.__name__ for cls in type(error).__mro__}
    who = provider_label or (f"The server at {_host(base_url)}" if base_url else "The model server")
    if status is None and names & {"APIConnectionError", "ConnectError", "ConnectTimeout"}:
        if "APITimeoutError" in names or "ConnectTimeout" in names:
            text = f"{who} did not answer in time ({_host(base_url)}). Is it running and reachable?"
        else:
            text = f"Could not connect to {_host(base_url)}. Is the server running?"
    elif status is None and names & {"APITimeoutError", "ReadTimeout", "TimeoutError", "WriteTimeout", "PoolTimeout"}:
        text = f"{who} took too long to respond. Try again, or raise the timeout in Settings."
    else:
        if not message:
            first = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
            message = re.sub(r"^Error code: \d+\s*-\s*", "", first)
        message = re.sub(r"\s+", " ", message).strip()
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS].rstrip() + "…"
        hints = {
            401: " Check the API key.",
            403: " The key may lack access to this model.",
            404: " The model or URL may be wrong.",
            429: " Rate limit or quota reached; wait a moment and retry.",
        }
        head = f"{who} returned HTTP {status}" if status else f"{who} failed"
        if not message.endswith((".", "!", "?", "…")):
            message += "."
        text = f"{head}: {message}"
        text += hints.get(status, "")
    return text, status, _raw_text(error, status)
