"""Request guards: Host/Origin checks, security headers, token auth.

The agent can run shell commands, so every API call needs the per-run token
(cookie or Bearer header) and the server only answers requests addressed to
the host it is bound to (DNS-rebinding defense).
"""

import secrets
from urllib.parse import urlparse

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse

COOKIE_NAME = "jarvis_token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
)


def is_loopback(host):
    return host in LOOPBACK_HOSTS


def allowed_hosts_for(host, port):
    """Host header values accepted when bound to loopback; None = any (remote mode)."""
    if not is_loopback(host):
        return None
    names = {"127.0.0.1", "localhost", "[::1]"}
    return {f"{name}:{port}" for name in names}


def token_matches(supplied, token):
    return bool(supplied) and secrets.compare_digest(str(supplied), str(token))


def request_token_ok(request, token):
    """True when the cookie or a Bearer header carries the run token."""
    if token_matches(request.cookies.get(COOKIE_NAME), token):
        return True
    auth = request.headers.get("authorization", "")
    return auth.lower().startswith("bearer ") and token_matches(auth[7:].strip(), token)


class SecurityMiddleware:
    """Pure-ASGI middleware (safe for streaming responses such as SSE)."""

    def __init__(self, app, allowed_hosts=None):
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CSP
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Cache-Control"] = "no-store"
            await send(message)

        headers = Headers(scope=scope)
        host = headers.get("host", "").lower()
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            await PlainTextResponse("Bad Host header", status_code=400)(scope, receive, send_with_headers)
            return
        if scope["method"] in UNSAFE_METHODS:
            origin = headers.get("origin")
            if origin and urlparse(origin).netloc.lower() != host:
                await PlainTextResponse("Cross-origin request refused", status_code=403)(
                    scope, receive, send_with_headers)
                return
        await self.app(scope, receive, send_with_headers)
