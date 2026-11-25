"""OpenAI-compatible Chat Completions backend (LM Studio, Ollama, vLLM, ...)."""

import os

from openai import OpenAI


def resolve_key(llm):
    """Literal keys (session-local, never persisted) win over env vars."""
    return llm.get("api_key") or os.environ.get(llm.get("api_key_env") or "") or "not-needed"


class ChatBackend:
    """Streams or blocks one Chat Completions request; no retry logic here."""

    name = "chat"

    def __init__(self, llm):
        self.base_url = llm["base_url"]
        self.model = llm["model"]
        self.stream = llm["stream"]
        self.max_tokens = llm["max_tokens"]
        self.client = OpenAI(
            base_url=llm["base_url"],
            api_key=resolve_key(llm),
            timeout=float(llm.get("timeout", 120)),
            max_retries=0,
        )

    def list_models(self, timeout=5.0):
        """Sorted model ids the endpoint exposes (raises on failure)."""
        page = self.client.models.list(timeout=timeout)
        return sorted(model.id for model in page.data)

    def check_model(self, ids, timeout=5.0):
        """Send a 1-token request so a bad model/key fails now, not mid-turn."""
        self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            stream=False,
            timeout=timeout,
        )

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        kwargs = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if tools:
            kwargs["tools"] = tools
        if not self.stream:
            return self._blocking(kwargs, on_text, on_reasoning)
        return self._streaming(kwargs, on_text, on_reasoning)

    def _blocking(self, kwargs, on_text, on_reasoning):
        choice = self.client.chat.completions.create(**kwargs).choices[0].message
        msg = {"role": "assistant", "content": choice.content or ""}
        reasoning = _reasoning_of(choice)
        if reasoning:
            msg["reasoning"] = reasoning
            if on_reasoning:
                on_reasoning(reasoning)
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

    def _streaming(self, kwargs, on_text, on_reasoning):
        text_parts = []
        reasoning_parts = []
        tool_calls = {}  # index -> accumulating call dict
        try:
            for chunk in self.client.chat.completions.create(stream=True, **kwargs):
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                reasoning = _reasoning_of(delta)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    if on_reasoning:
                        on_reasoning(reasoning)
                if delta.content:
                    text_parts.append(delta.content)
                    if on_text:
                        on_text(delta.content)
                for tc in delta.tool_calls or []:
                    idx = tc.index if tc.index is not None else len(tool_calls)
                    slot = tool_calls.setdefault(idx, {
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
            try:
                error._jarvis_partial = bool(text_parts or tool_calls)  # type: ignore[attr-defined]
            except Exception:
                pass
            raise

        msg = {"role": "assistant", "content": "".join(text_parts)}
        if reasoning_parts:
            msg["reasoning"] = "".join(reasoning_parts)
        if tool_calls:
            # Some local servers omit tool-call IDs; synthesize them.
            for i in sorted(tool_calls):
                if not tool_calls[i]["id"]:
                    tool_calls[i]["id"] = f"call_{i}"
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return msg


def _reasoning_of(obj):
    """Reasoning text from a delta/message.

    LM Studio, DeepSeek and vLLM use ``reasoning_content``; OpenRouter uses
    ``reasoning``. The SDK keeps unknown fields as extras, so getattr works.
    """
    return getattr(obj, "reasoning_content", None) or getattr(obj, "reasoning", None) or ""
