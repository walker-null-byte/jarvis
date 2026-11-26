"""LLM facade: picks a backend from the provider preset and adds retries.

Backends (jarvis/providers/):
  chat      OpenAI-compatible Chat Completions (LM Studio, Ollama, vLLM, ...)
  chatgpt   Sign in with ChatGPT -> Responses API
  anthropic official anthropic SDK
  cloudflare Workers AI / AI Gateway (Chat Completions)

Every backend exposes ``chat(messages, tools, on_text, on_reasoning)``,
``list_models(timeout)`` and ``check_model(ids, timeout)``.
"""

import re
import time

from .providers import PRESETS, backend_for
from .providers.errors import Cancelled, LLMError, _dig_message, explain_error
from .providers.openai_chat import ChatBackend, resolve_key

__all__ = ["LLM", "LLMError", "Cancelled"]

_RETRYABLE_NAMES = {
    "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
    "ReadTimeout", "ReadError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
}


def _build_backend(name, llm):
    """Create a backend by name; SDK imports happen here, lazily."""
    if name == "anthropic":
        from .providers.anthropic_native import AnthropicBackend
        return AnthropicBackend(llm)
    if name == "chatgpt":
        from .providers.chatgpt import ChatGPTBackend
        return ChatGPTBackend(llm)
    if name == "cloudflare":
        from .providers.cloudflare import CloudflareBackend
        return CloudflareBackend(llm)
    return ChatBackend(llm)


def is_retryable(error):
    """Retry only connection errors, timeouts, HTTP 429 and 5xx."""
    if getattr(error, "_jarvis_partial", False):
        return False
    explicit = getattr(error, "retryable", None)
    if explicit is not None:
        return bool(explicit)
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    return any(cls.__name__ in _RETRYABLE_NAMES for cls in type(error).__mro__)


class _Timing:
    """Wall-clock marks for one request, taken from the streaming callbacks."""

    def __init__(self):
        self.start = time.monotonic()
        self.first = None            # first text or reasoning token
        self.reasoning_start = None
        self.reasoning_end = None

    def wrap(self, on_text, on_reasoning):
        def text(token):
            now = time.monotonic()
            if self.first is None:
                self.first = now
            if self.reasoning_start is not None and self.reasoning_end is None:
                self.reasoning_end = now
            if on_text:
                on_text(token)

        def reasoning(token):
            now = time.monotonic()
            if self.first is None:
                self.first = now
            if self.reasoning_start is None:
                self.reasoning_start = now
            if on_reasoning:
                on_reasoning(token)

        return text, reasoning

    def stats(self, msg, model, streamed):
        """Speed/usage numbers for the finished reply (None-valued when unknown)."""
        end = time.monotonic()
        usage = msg.pop("usage", None) or {}
        reported = usage.get("output")
        if isinstance(reported, int):
            tokens, approx = reported, False
        else:
            chars = len(msg.get("content") or "") + len(msg.get("reasoning") or "")
            for call in msg.get("tool_calls") or []:
                chars += len(call["function"].get("arguments") or "") + len(call["function"].get("name") or "")
            tokens, approx = max(1, round(chars / 4)) if chars else 0, True
        total = end - self.start
        ttft = (self.first - self.start) if self.first is not None else None
        gen = (end - self.first) if (streamed and self.first is not None) else None
        rate = round(tokens / gen, 1) if (gen and gen >= 0.05 and tokens) else None
        thinking = None
        if self.reasoning_start is not None:
            thinking = (self.reasoning_end or end) - self.reasoning_start
        return {
            "model": model,
            "input_tokens": usage.get("input"),
            "output_tokens": tokens,
            "approx": approx,
            "ttft": round(ttft, 2) if ttft is not None else None,
            "gen_seconds": round(gen, 2) if gen is not None else None,
            "seconds": round(total, 2),
            "tok_per_s": rate,
            "thinking_seconds": round(thinking, 1) if thinking is not None else None,
        }


class LLM:
    def __init__(self, config):
        self.last_error = None
        self.reconfigure(config)

    @staticmethod
    def _resolve_key(llm):
        """Literal keys (session-local, never persisted) win over env vars."""
        return resolve_key(llm)

    @property
    def client(self):
        """The chat backend's OpenAI client (kept for tests/back-compat)."""
        return getattr(self._impl, "client", None)

    @client.setter
    def client(self, value):
        self._impl.client = value

    def reconfigure(self, config):
        """Apply runtime LLM settings without rebuilding the agent."""
        llm = config["llm"]
        self.provider = llm.get("provider", "custom")
        self.backend = backend_for(self.provider)
        self.base_url = llm["base_url"]
        self.api_key_env = llm.get("api_key_env")
        self.api_key = self._resolve_key(llm)
        self.model = llm["model"]
        self.stream = llm["stream"]
        self.max_tokens = llm["max_tokens"]
        self.timeout = float(llm.get("timeout", 120))
        self.retries = max(0, int(llm.get("retries", 2)))
        self.retry_delay = max(0.0, float(llm.get("retry_delay", 1.0)))
        self.last_error = None
        self._impl = _build_backend(self.backend, llm)

    def probe(self, timeout=5.0, check_model=True):
        """Check reachability and list the models an endpoint exposes.

        Returns ``(ok, message, model_ids, http_status)`` and never raises.
        With *check_model* (default) it also verifies the configured model
        (chat: a 1-token request; anthropic: the id must be listed; chatgpt:
        skipped) so a wrong model or missing key is reported before the first
        real turn. *message* extracts the server's own error text and hints at
        the classic missing ``/v1`` path mistake when the listing 404s.
        """
        try:
            ids = self._impl.list_models(timeout=timeout)
        except Exception as error:
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            return False, _probe_error_message(error, self.base_url, v1_hint=self.backend == "chat"), [], status
        if check_model and self.model:
            try:
                self._impl.check_model(ids, timeout=timeout)
            except Exception as error:
                status = getattr(error, "status_code", None) or getattr(error, "status", None)
                return (
                    False,
                    (
                        f"Connected — {len(ids)} model(s) listed; "
                        f"model {self.model!r} rejected: "
                        f"{_probe_error_message(error, self.base_url)}"
                    ),
                    ids,
                    status,
                )
        return True, f"Connected — {len(ids)} model(s) listed", ids, 200

    def output_limit(self, override=None):
        """The output-token limit a request will use (None when the backend sets none)."""
        fn = getattr(self._impl, "effective_max_tokens", None)
        return fn(override) if fn else (override or self.max_tokens)

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None, max_tokens=None):
        """Send one request; returns an assistant message dict (see CONTRACT).

        The message carries ``stop`` (why the model stopped: "end", "tool_calls", "length",
        "content_filter", "refusal", or the provider's own word) when the backend reported it.
        *max_tokens* overrides the configured output limit for this one request.

        on_text(token) / on_reasoning(token) receive streamed fragments.
        Retries connection errors, timeouts, 429 and 5xx with exponential
        backoff, never other 4xx and never after partial output.
        """
        for attempt in range(self.retries + 1):
            timing = _Timing()
            on_text_t, on_reasoning_t = timing.wrap(on_text, on_reasoning)
            try:
                self.last_error = None
                extra = {"max_tokens": max_tokens} if max_tokens else {}
                msg = self._impl.chat(messages, tools, on_text_t, on_reasoning_t, **extra)
            except Cancelled:
                raise
            except Exception as error:
                self.last_error = error
                if attempt >= self.retries or not is_retryable(error):
                    wrapped = self._wrap_error(error)
                    raise wrapped from (None if wrapped is error else error)
                time.sleep(self.retry_delay * (2 ** attempt))
                continue
            msg["stats"] = timing.stats(msg, self.model, self.stream if self.backend in ("chat", "cloudflare") else True)
            return msg

    def _wrap_error(self, error):
        """A readable LLMError (message, status, raw details) for any failure."""
        if isinstance(error, LLMError):
            if not error.provider:
                error.provider = self.provider
            return error
        label = (PRESETS.get(self.provider, {}).get("label") or "").split(" — ")[0]  # custom servers: named by host
        message, status, details = explain_error(error, label, self.base_url)
        hint = getattr(self._impl, "hints", {}).get(status)
        if hint:  # replace the generic hint with the backend's more specific one
            message = message.removesuffix(" Check the API key.") + hint
        return LLMError(message, status=status, details=details, provider=self.provider)


def _probe_error_message(error, base_url, v1_hint=False):
    """Turn a probe exception into a short, actionable message."""
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if response is not None:
        try:
            data = response.json()
            message = _dig_message(data) or str(error)
        except Exception:
            text = response.text or ""
            match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
            message = match.group(1).strip() if match else str(error)
    else:
        message = str(error)
    if status:
        message = f"HTTP {status}: {message}"
    if v1_hint and status == 404 and not str(base_url).rstrip("/").endswith("/v1"):
        message += " — the server may expect a base URL ending in /v1"
    return message
