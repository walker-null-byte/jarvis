"""Sign in with ChatGPT (OAuth 2.0 + PKCE, loopback redirect).

Follows OpenAI's "token sharing for open-source apps" docs. A user with a
ChatGPT Plus/Pro plan signs in once in the browser; Jarvis then holds an
access token (1 h) and a rotating refresh token (30 days) and calls the
Responses API with the user's plan (see chatgpt.py).

Credentials live in $XDG_CONFIG_HOME/jarvis/chatgpt_auth.json (0600, dir
0700). Tokens are never logged and never put in URLs or events.
"""

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .errors import LLMError

ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLACEHOLDER_CLIENT_ID = "dynamic_agent_client"  # never saved
REDIRECT_HOST = "127.0.0.1"  # not "localhost"
REDIRECT_PORT = 1455
REDIRECT_PATH = "/auth/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
REFRESH_MARGIN = 120  # seconds
RELOGIN_MESSAGE = "ChatGPT session expired — run /login chatgpt"
# Token-endpoint error codes that mean the refresh token is dead for good.
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
}

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Jarvis</title>"
    b"<body style='font-family:sans-serif;text-align:center;margin-top:20vh'>"
    b"<h2>Signed in &mdash; you can close this tab.</h2></body>"
)


def auth_file_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jarvis" / "chatgpt_auth.json"


def pkce_challenge(verifier):
    """S256 code challenge: base64url(SHA-256(verifier)) with no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def decode_jwt_payload(token):
    """Decode a JWT payload WITHOUT verifying the signature.

    Skipping verification is allowed here (OIDC Core 3.1.3.7): the ID token
    comes straight from the token endpoint over TLS, not from the browser.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as error:
        raise LLMError("ChatGPT sign-in returned an unreadable ID token") from error


class ChatGPTAuth:
    def __init__(self, config=None, path=None, transport=None):
        """*path* and *transport* exist for tests (tmp file, fake httpx transport)."""
        self.config = config
        self.path = Path(path) if path else auth_file_path()
        self._transport = transport
        self._lock = threading.RLock()  # serializes refresh + file writes
        self._attempt = None  # the login in flight, if any
        self._error = ""

    # ---- storage -------------------------------------------------------

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        """Atomic write with 0600 perms (dir 0700)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".chatgpt_auth.")
        try:
            with os.fdopen(fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _host_id(self, data):
        """Per-host id, generated once and persisted."""
        if not data.get("host_id"):
            data["host_id"] = f"urn:uuid:{uuid.uuid4()}"
            self._save(data)
        return data["host_id"]

    def _clear_tokens(self):
        """Drop tokens but keep client_id, host_id and email (for login_hint)."""
        with self._lock:
            data = self._load()
            data.pop("tokens", None)
            self._save(data)

    # ---- public API ----------------------------------------------------

    def status(self):
        data = self._load()
        tokens = data.get("tokens") or {}
        signed_in = bool(tokens.get("refresh_token"))
        scopes = str(tokens.get("scope", "")).split()
        plan_ok = signed_in and REQUIRED_SCOPE in scopes
        error = self._error
        if signed_in and not plan_ok and not error:
            error = f"Granted scopes lack {REQUIRED_SCOPE}; ChatGPT Plus or Pro is required"
        return {
            "signed_in": signed_in,
            "email": data.get("email", "") if signed_in else "",
            "plan_ok": plan_ok,
            "expires_at": tokens.get("expires_at") if signed_in else None,
            "pending": self._attempt is not None and not self._attempt["done"].is_set(),
            "error": error,
        }

    def start_login(self, open_browser=True):
        """Start the browser sign-in; returns {"auth_url"} immediately."""
        with self._lock:
            attempt = self._attempt
            if attempt is not None and not attempt["done"].is_set():
                return {"auth_url": attempt["auth_url"]}
            self._error = ""
            data = self._load()
            attempt = self._new_attempt(data)
            # The listener must be up BEFORE the browser opens.
            attempt["server"] = self._start_listener(attempt)
            self._attempt = attempt
        threading.Thread(target=self._finish_login, args=(attempt,), daemon=True).start()
        if open_browser:
            try:
                webbrowser.open(attempt["auth_url"])
            except Exception:
                pass
        return {"auth_url": attempt["auth_url"]}

    def wait(self, timeout=300):
        """Block until the login in flight finishes; returns status()."""
        attempt = self._attempt
        if attempt is not None:
            attempt["done"].wait(timeout)
        return self.status()

    def logout(self):
        """Revoke the refresh token (best effort) and delete stored tokens."""
        with self._lock:
            data = self._load()
            tokens = data.get("tokens") or {}
            if tokens.get("refresh_token") and data.get("client_id"):
                try:
                    self._revoke(tokens["refresh_token"], data["client_id"])
                except Exception:
                    pass  # local sign-out still happens
            data.pop("tokens", None)
            self._save(data)
            self._error = ""
        return self.status()

    def access_token(self):
        """A valid access token, refreshing first when < 120 s remain."""
        with self._lock:
            data = self._load()
            tokens = data.get("tokens") or {}
            if not tokens.get("refresh_token"):
                raise LLMError("Not signed in to ChatGPT — run /login chatgpt", retryable=False)
            if REQUIRED_SCOPE not in str(tokens.get("scope", "")).split():
                raise LLMError(
                    f"ChatGPT sign-in lacks {REQUIRED_SCOPE}; this account needs Plus or Pro",
                    retryable=False,
                )
            if float(tokens.get("expires_at") or 0) - time.time() < REFRESH_MARGIN:
                data = self._refresh(data)
                tokens = data["tokens"]
            return tokens["access_token"]

    # ---- login flow ----------------------------------------------------

    def _new_attempt(self, data):
        """Fresh state/nonce/verifier for one sign-in attempt."""
        client_id = data.get("client_id") or ""
        attempt = {
            "first": not client_id,
            "client_id": client_id or PLACEHOLDER_CLIENT_ID,
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "verifier": secrets.token_urlsafe(64),
            "params": None,
            "received": threading.Event(),
            "done": threading.Event(),
        }
        attempt["auth_url"] = self.build_authorize_url(attempt, data)
        return attempt

    def build_authorize_url(self, attempt, data=None):
        data = self._load() if data is None else data
        params = {
            "client_id": attempt["client_id"],
            "ext_agent_host_id": self._host_id(data),
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "resource": RESOURCE,
            "state": attempt["state"],
            "nonce": attempt["nonce"],
            "code_challenge_method": "S256",
            "code_challenge": pkce_challenge(attempt["verifier"]),
        }
        if attempt["first"]:
            params["agent_name_hint"] = "Jarvis"  # first registration only
        else:
            id_token = (data.get("tokens") or {}).get("id_token") or data.get("id_token")
            if id_token:
                params["id_token_hint"] = id_token
            if data.get("email"):
                params["login_hint"] = data["email"]
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def _start_listener(self, attempt):
        def handler_factory(*args, **kwargs):
            return _CallbackHandler(attempt, *args, **kwargs)

        try:
            server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), handler_factory)
        except OSError as error:
            raise LLMError(
                f"Cannot listen on {REDIRECT_HOST}:{REDIRECT_PORT} for the ChatGPT sign-in "
                f"callback ({error}). Close whatever uses that port and retry."
            ) from error
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _finish_login(self, attempt):
        """Worker: wait for the browser callback, then exchange the code."""
        try:
            if not attempt["received"].wait(300):
                raise LLMError("ChatGPT sign-in timed out")
            self.complete_login(attempt, attempt["params"])
        except Exception as error:
            self._error = str(error)
        finally:
            server = attempt.get("server")
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()
                server.server_close()
            attempt["done"].set()

    def complete_login(self, attempt, params):
        """Validate the callback *params*, exchange the code, store tokens."""
        # State first, before looking at anything else.
        if not secrets.compare_digest(str(params.get("state", "")), attempt["state"]):
            raise LLMError("ChatGPT sign-in failed: state mismatch")
        if params.get("error") == "access_denied":
            raise LLMError("ChatGPT sign-in was cancelled")
        if params.get("error"):
            raise LLMError(f"ChatGPT sign-in failed: {params['error']}")
        code = params.get("code")
        if not code:
            raise LLMError("ChatGPT sign-in failed: no authorization code returned")

        returned = params.get("client_id") or ""
        with self._lock:
            data = self._load()
            if attempt["first"]:
                if not returned or returned == PLACEHOLDER_CLIENT_ID:
                    raise LLMError("ChatGPT sign-in failed: no client_id issued")
                client_id = returned
            else:
                client_id = attempt["client_id"]
                if returned and returned != client_id:
                    raise LLMError("ChatGPT sign-in failed: client_id changed")

            body = {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": attempt["verifier"],
                "redirect_uri": REDIRECT_URI,
                "resource": RESOURCE,
            }
            token = self._token_request(body)  # invalid_grant -> raises; caller restarts
            claims = decode_jwt_payload(token.get("id_token", ""))
            aud = claims.get("aud")
            audiences = aud if isinstance(aud, list) else [aud]
            if claims.get("iss") != ISSUER:
                raise LLMError("ChatGPT sign-in failed: unexpected token issuer")
            if client_id not in audiences:
                raise LLMError("ChatGPT sign-in failed: token audience mismatch")
            if float(claims.get("exp") or 0) < time.time():
                raise LLMError("ChatGPT sign-in failed: ID token expired")
            if claims.get("nonce") != attempt["nonce"]:
                raise LLMError("ChatGPT sign-in failed: nonce mismatch")

            data["client_id"] = client_id  # the issued id; never the placeholder
            data["sub"] = claims.get("sub", "")
            data["email"] = claims.get("email") or data.get("email", "")
            data["tokens"] = self._tokens_from(token, requested_scope=SCOPE)
            data["tokens"]["id_token"] = token["id_token"]
            self._save(data)
            if REQUIRED_SCOPE not in data["tokens"]["scope"].split():
                self._error = (
                    f"Granted scopes lack {REQUIRED_SCOPE}; ChatGPT Plus or Pro is required"
                )

    # ---- token endpoint ------------------------------------------------

    def _client(self):
        return httpx.Client(transport=self._transport, timeout=20.0)

    @staticmethod
    def _tokens_from(token, requested_scope=""):
        if not token.get("access_token") or not token.get("refresh_token"):
            raise LLMError("ChatGPT token response is missing tokens")
        return {
            "access_token": token["access_token"],
            "refresh_token": token["refresh_token"],
            "expires_at": time.time() + float(token.get("expires_in") or 3600),
            "scope": token.get("scope") or requested_scope,
        }

    def _token_request(self, body):
        """POST a form to the token endpoint; returns the JSON dict."""
        try:
            with self._client() as client:
                response = client.post(TOKEN_URL, data=body)
        except httpx.HTTPError as error:
            raise LLMError(f"ChatGPT token request failed: {error}", retryable=True) from error
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400 or "error" in payload:
            code = payload.get("error")
            if isinstance(code, dict):
                code = code.get("code")
            code = str(code or f"http_{response.status_code}")
            raise LLMError(
                f"ChatGPT token request failed: {code}",
                retryable=response.status_code >= 500 or response.status_code == 429,
                status=response.status_code,
            ) from _TokenError(code)
        return payload

    def _refresh(self, data):
        """Rotate tokens. Caller holds the lock. Returns the updated data."""
        tokens = data["tokens"]
        body = {
            "grant_type": "refresh_token",
            "client_id": data.get("client_id", ""),
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,  # no scope on refresh
        }
        try:
            token = self._token_request(body)
        except LLMError as error:
            cause = error.__cause__
            if isinstance(cause, _TokenError) and cause.code in TERMINAL_REFRESH_ERRORS:
                self._clear_tokens()
                raise LLMError(RELOGIN_MESSAGE, retryable=False) from error
            raise
        # New refresh token + access token + expiry + scopes saved together.
        new_tokens = self._tokens_from(token, requested_scope=tokens.get("scope", ""))
        new_tokens["id_token"] = token.get("id_token") or tokens.get("id_token", "")
        data["tokens"] = new_tokens
        self._save(data)
        return data

    def _revoke(self, refresh_token, client_id):
        with self._client() as client:
            endpoint = client.get(DISCOVERY_URL).json().get("revocation_endpoint")
            if endpoint:
                client.post(endpoint, data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": client_id,
                })


class _TokenError(Exception):
    """Carries the OAuth error code from a failed token request."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _CallbackHandler(BaseHTTPRequestHandler):
    """Receives the single OAuth redirect on /auth/callback."""

    def __init__(self, attempt, *args, **kwargs):
        self.attempt = attempt
        super().__init__(*args, **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != REDIRECT_PATH:
            self.send_error(404)
            return
        self.attempt["params"] = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_DONE_PAGE)))
        self.end_headers()
        self.wfile.write(_DONE_PAGE)
        self.attempt["received"].set()

    def log_message(self, *args):
        pass  # the query string holds the auth code; never log it


_singleton = None
_singleton_lock = threading.Lock()


def get_chatgpt_auth(config=None):
    """Process-wide ChatGPTAuth."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ChatGPTAuth(config)
        return _singleton
