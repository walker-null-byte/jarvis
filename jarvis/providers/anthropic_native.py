"""Native Anthropic backend using the official `anthropic` SDK (optional extra)."""

import json
import re

from .errors import LLMError
from .openai_chat import resolve_key

DEFAULT_BASE_URL = "https://api.anthropic.com"
THINKING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MIN_MAX_TOKENS = 32000
# Models that get server-side refusal fallbacks.
FALLBACK_MODELS = {
    "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
}


def supports_thinking(model):
    """Adaptive thinking exists on opus 4.6+/5, sonnet 4.6+/5, haiku 5, fable, mythos.

    It is skipped for claude-haiku-4-5 and claude-3*.
    """
    if model.startswith(("claude-fable", "claude-mythos", "claude-haiku-5")):
        return True
    match = re.match(r"claude-(opus|sonnet)-(\d+)(?:-(\d{1,2}))?(?!\d)", model)
    if not match:
        return False
    major, minor = int(match.group(2)), int(match.group(3) or 0)
    return major > 4 or (major == 4 and minor >= 6)


def to_anthropic_messages(messages, model):
    """Convert chat history to ``(system_text, anthropic_messages)``."""
    system = []
    out = []

    def add(role, blocks):
        if not blocks:
            return
        if out and out[-1]["role"] == role:  # consecutive same-role turns merge
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "system":
            if content:
                system.append(content)
        elif role == "user":
            add("user", [{"type": "text", "text": content}] if content.strip() else [])
        elif role == "tool":
            add("user", [{
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content if isinstance(content, str) else str(content),
            }])
        elif role == "assistant":
            state = msg.get("provider_state") or {}
            if state.get("backend") == "anthropic" and state.get("model") == model and state.get("items"):
                add("assistant", list(state["items"]))  # raw blocks, thinking included
                continue
            blocks = []
            if content.strip():
                blocks.append({"type": "text", "text": content})
            for call in msg.get("tool_calls") or []:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "input": args if isinstance(args, dict) else {},
                })
            add("assistant", blocks)
    return "\n\n".join(system), out


def to_anthropic_tools(tools):
    return [
        {
            "name": (t.get("function", t))["name"],
            "description": (t.get("function", t)).get("description", ""),
            "input_schema": (t.get("function", t)).get("parameters") or {"type": "object", "properties": {}},
            "eager_input_streaming": True,
        }
        for t in tools or []
    ]


def _dump(block):
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return block.to_dict()


def final_to_message(final, model):
    """Convert an SDK final message to the contract's assistant dict."""
    stop = getattr(final, "stop_reason", None)
    if stop == "refusal":  # check before touching content
        details = getattr(final, "stop_details", None)
        category = getattr(details, "category", None)
        text = "The model declined this request."
        if category:
            text += f" (category: {category})"
        return {"role": "assistant", "content": text}

    text_parts, thinking_parts, tool_calls, items = [], [], [], []
    for block in final.content:
        kind = getattr(block, "type", "")
        items.append(_dump(block))
        if kind == "text":
            text_parts.append(block.text)
        elif kind == "thinking":
            thinking_parts.append(getattr(block, "thinking", "") or "")
        elif kind == "tool_use":
            tool_calls.append({
                "id": block.id,
                "type": "function",
                # Inputs stream unvalidated; Jarvis validates against the schema.
                "function": {"name": block.name, "arguments": json.dumps(block.input or {})},
            })
    content = "".join(text_parts)
    if stop == "max_tokens":
        content += "\n\n[response truncated]"
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    reasoning = "".join(thinking_parts)
    if reasoning:
        msg["reasoning"] = reasoning
    if items:
        msg["provider_state"] = {"backend": "anthropic", "model": model, "items": items}
    usage = getattr(final, "usage", None)
    out_tokens = getattr(usage, "output_tokens", None)
    if isinstance(out_tokens, int):
        inp = getattr(usage, "input_tokens", None)
        msg["usage"] = {"input": inp if isinstance(inp, int) else None, "output": out_tokens}
    return msg


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, llm):
        self.model = llm["model"]
        self.base_url = llm.get("base_url") or DEFAULT_BASE_URL
        self.api_key = resolve_key(llm)
        self.timeout = float(llm.get("timeout", 120))
        self.max_tokens = int(llm.get("max_tokens") or 0)
        self.effort = llm.get("effort") or ""
        self._client_obj = None

    def _client(self):
        if self._client_obj is None:
            try:
                import anthropic
            except ImportError as error:
                raise LLMError("The anthropic package is missing; reinstall Jarvis: pip install -e .") from error
            if self.api_key == "not-needed":
                raise LLMError("No Anthropic API key — set ANTHROPIC_API_KEY", retryable=False)
            kwargs = {"api_key": self.api_key, "timeout": self.timeout, "max_retries": 0}
            if self.base_url.rstrip("/") != DEFAULT_BASE_URL:
                kwargs["base_url"] = self.base_url
            self._client_obj = anthropic.Anthropic(**kwargs)
        return self._client_obj

    def list_models(self, timeout=5.0):
        page = self._client().models.list(limit=1000, timeout=timeout)
        return sorted(model.id for model in page)

    def check_model(self, ids, timeout=5.0):
        """The configured id must be listed (no billed ping)."""
        if self.model not in ids:
            raise ValueError(f"model {self.model!r} is not in the account's model list")

    def build_request(self, messages, tools=None):
        """Keyword arguments for ``client.beta.messages.stream``."""
        system, converted = to_anthropic_messages(messages, self.model)
        kwargs = {
            "model": self.model,
            "max_tokens": max(self.max_tokens, MIN_MAX_TOKENS),
            "messages": converted,
            "cache_control": {"type": "ephemeral"},
            "betas": [],
        }
        if system:
            kwargs["system"] = [{"type": "text", "text": system}]
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools)
        if supports_thinking(self.model):
            kwargs["thinking"] = {
                "type": "adaptive",
                "display": "summarized",
                "block_binding": {"prefix_mismatch_behavior": "drop_block"},
            }
            kwargs["betas"].append(THINKING_BETA)
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.model in FALLBACK_MODELS:
            kwargs["fallbacks"] = "default"
            kwargs["betas"].append(FALLBACK_BETA)
        if not kwargs["betas"]:
            del kwargs["betas"]
        return kwargs

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        client = self._client()
        kwargs = self.build_request(messages, tools)
        got_output = False
        try:
            with client.beta.messages.stream(**kwargs) as stream:
                for event in stream:
                    if getattr(event, "type", "") != "content_block_delta":
                        continue
                    delta = event.delta
                    if delta.type == "text_delta":
                        got_output = True
                        if on_text:
                            on_text(delta.text)
                    elif delta.type == "thinking_delta":
                        if on_reasoning:
                            on_reasoning(delta.thinking)
                    else:
                        got_output = True  # tool input etc.
                final = stream.get_final_message()
        except Exception as error:
            if got_output:
                try:
                    error._jarvis_partial = True  # type: ignore[attr-defined]
                except Exception:
                    pass
            raise
        return final_to_message(final, self.model)
