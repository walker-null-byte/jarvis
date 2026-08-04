"""OpenAI-compatible LLM client with streaming and tool-call support.

Works against LM Studio, Ollama, llama.cpp, vLLM, or Gemini's OpenAI endpoint —
whatever config.toml points at.
"""

import os
import re
import time

from openai import OpenAI


class LLMError(RuntimeError):
    """Normalized LLM request failure with the original cause attached."""


class LLM:
    def __init__(self, config):
        llm = config["llm"]
        self.base_url = llm["base_url"]
        self.api_key_env = llm["api_key_env"]
        self.api_key = self._resolve_key(llm)
        self.model = llm["model"]
        self.stream = llm["stream"]
        self.max_tokens = llm["max_tokens"]
        self.timeout = float(llm.get("timeout", 120))
        self.retries = max(0, int(llm.get("retries", 2)))
        self.retry_delay = max(0.0, float(llm.get("retry_delay", 1.0)))
        self.last_error = None
        self.client = OpenAI(
            base_url=llm["base_url"],
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=0,
        )

    @staticmethod
    def _resolve_key(llm):
        """Literal keys (session-local, never persisted) win over env vars."""
        return llm.get("api_key") or os.environ.get(llm.get("api_key_env") or "") or "not-needed"

    def reconfigure(self, config):
        """Apply runtime LLM settings without rebuilding the agent."""
        llm = config["llm"]
        self.base_url = llm["base_url"]
        self.api_key_env = llm["api_key_env"]
        self.api_key = self._resolve_key(llm)
        self.model = llm["model"]
        self.stream = llm["stream"]
        self.max_tokens = llm["max_tokens"]
        self.timeout = float(llm.get("timeout", 120))
        self.retries = max(0, int(llm.get("retries", 2)))
        self.retry_delay = max(0.0, float(llm.get("retry_delay", 1.0)))
        self.last_error = None
        self.client = OpenAI(
            base_url=llm["base_url"],
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=0,
        )

    def probe(self, timeout=5.0, check_model=True):
        """Check reachability and list the models an endpoint exposes.

        Returns ``(ok, message, model_ids, http_status)`` and never raises.
        With *check_model* (default), it also sends a 1-token chat request so
        a wrong model or missing API key is reported instead of failing on
        the first real turn. *message* extracts the server's own error text
        (even from HTML error pages) and hints at the classic missing
        ``/v1`` path mistake when the model listing itself 404s.
        """
        try:
            page = self.client.models.list(timeout=timeout)
            ids = sorted(model.id for model in page.data)
        except Exception as error:
            status = getattr(error, "status_code", None)
            return False, _probe_error_message(error, self.base_url, v1_hint=True), [], status
        if check_model and self.model:
            try:
                self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    stream=False,
                    timeout=timeout,
                )
            except Exception as error:
                status = getattr(error, "status_code", None)
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

    def chat(self, messages, tools=None, on_text=None):
        """Send one chat request; returns an assistant message dict.

        The returned dict is API-ready for the next request:
        {"role": "assistant", "content": str, "tool_calls": [...]?}

        on_text(token) is called for each streamed text fragment.
        """
        kwargs = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if tools:
            kwargs["tools"] = tools

        for attempt in range(self.retries + 1):
            try:
                self.last_error = None
                if not self.stream:
                    return self._chat_blocking(kwargs, on_text)
                return self._chat_streaming(kwargs, on_text)
            except Exception as error:
                self.last_error = error
                partial = bool(getattr(error, "_jarvis_partial", False))
                if attempt >= self.retries or partial:
                    raise LLMError(
                        f"LLM request failed after {attempt + 1} attempt(s): {error}"
                    ) from error
                time.sleep(self.retry_delay * (2 ** attempt))

    def _chat_blocking(self, kwargs, on_text):
        choice = self.client.chat.completions.create(**kwargs).choices[0].message
        msg = {"role": "assistant", "content": choice.content or ""}
        if choice.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id or f"call_{i}",
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments or ""},
                }
                for i, tc in enumerate(choice.tool_calls)
            ]
        if on_text and msg["content"]:
            on_text(msg["content"])
        return msg

    def _chat_streaming(self, kwargs, on_text):
        text_parts = []
        tool_calls = {}  # index -> accumulating call dict
        try:
            for chunk in self.client.chat.completions.create(stream=True, **kwargs):
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    text_parts.append(delta.content)
                    if on_text:
                        on_text(delta.content)
                for tc in delta.tool_calls or []:
                    slot = tool_calls.setdefault(tc.index, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["function"]["arguments"] += tc.function.arguments
        except Exception as error:
            error._jarvis_partial = bool(text_parts or tool_calls)
            raise

        msg = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            # Some local servers omit tool-call IDs; synthesize them.
            for i in sorted(tool_calls):
                if not tool_calls[i]["id"]:
                    tool_calls[i]["id"] = f"call_{i}"
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return msg


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
