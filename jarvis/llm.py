"""LLM facade: picks a backend from the provider preset and adds retries.

Backends (jarvis/providers/):
  chat      OpenAI-compatible Chat Completions (LM Studio, Ollama, vLLM, ...)
  chatgpt   Sign in with ChatGPT -> Responses API
  anthropic official anthropic SDK

Every backend exposes ``chat(messages, tools, on_text, on_reasoning)``,
``list_models(timeout)`` and ``check_model(ids, timeout)``.
"""

import re
import time

from .providers import backend_for
from .providers.errors import LLMError
from .providers.openai_chat import ChatBackend, resolve_key

__all__ = ["LLM", "LLMError"]

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

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        """Send one request; returns an assistant message dict (see CONTRACT).

        on_text(token) / on_reasoning(token) receive streamed fragments.
        Retries connection errors, timeouts, 429 and 5xx with exponential
        backoff, never other 4xx and never after partial output.
        """
        for attempt in range(self.retries + 1):
            try:
                self.last_error = None
                return self._impl.chat(messages, tools, on_text, on_reasoning)
            except Exception as error:
                self.last_error = error
                if attempt >= self.retries or not is_retryable(error):
                    if isinstance(error, LLMError):
                        raise
                    raise LLMError(
                        f"LLM request failed after {attempt + 1} attempt(s): {error}"
                    ) from error
                time.sleep(self.retry_delay * (2 ** attempt))


def _probe_error_message(error, base_url, v1_hint=False):
    """Turn a probe exception into a short, actionable message."""
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if response is not None:
        try:
            data = response.json()
            message = (
                data.get("error", {}).get("message")
                or data.get("message")
                or str(error)
            )
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
