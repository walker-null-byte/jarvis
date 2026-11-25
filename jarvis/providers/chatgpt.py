"""ChatGPT-plan backend: the Responses API with a "Sign in with ChatGPT" token.

Request restrictions (per OpenAI's docs): input must be an array, no system
items (the system prompt goes in ``instructions``), and no max_output_tokens,
temperature, top_p, metadata, user, truncation, previous_response_id,
background or prompt. A fresh client is built per request with a current
access token.
"""

import httpx
from openai import OpenAI

from .chatgpt_auth import RESOURCE, get_chatgpt_auth
from .errors import LLMError

USAGE_LIMIT_MESSAGE = "ChatGPT plan usage limit reached — check https://chatgpt.com/settings/usage"
NOT_ELIGIBLE_MESSAGE = "This ChatGPT account isn't eligible (needs Plus or Pro)"


def to_responses_input(messages):
    """Convert chat history to ``(instructions, input_items)``."""
    instructions = []
    items = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "system":
            if content:
                instructions.append(content)
        elif role == "user":
            items.append({"role": "user", "content": content})
        elif role == "assistant":
            if content:
                items.append({"role": "assistant", "content": content})
            for call in msg.get("tool_calls") or []:
                items.append({
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"].get("arguments") or "{}",
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": content if isinstance(content, str) else str(content),
            })
    return "\n\n".join(instructions), items


def to_responses_tools(tools):
    """OpenAI chat tool format -> Responses function tools."""
    out = []
    for tool in tools or []:
        fn = tool.get("function", tool)
        out.append({
            "type": "function",
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def error_from_code(code, status=None, message=""):
    """Map a ChatGPT-plan error code to an LLMError (None if unknown)."""
    code = code or ""
    if code == "subscription_sharing_usage_limit_exceeded":
        return LLMError(USAGE_LIMIT_MESSAGE, retryable=False, status=status)
    if code == "subscription_sharing_user_not_eligible":
        return LLMError(NOT_ELIGIBLE_MESSAGE, retryable=False, status=status)
    if code == "subscription_sharing_usage_unavailable":
        return LLMError("ChatGPT usage info is temporarily unavailable", retryable=True, status=status)
    if code == "subscription_sharing_invalid_user":
        get_chatgpt_auth()._clear_tokens()
        return LLMError("ChatGPT sign-in is no longer valid — run /login chatgpt", retryable=False, status=status)
    return None


def map_api_error(error):
    """Turn an SDK/HTTP exception into an LLMError when we recognise it."""
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    code = getattr(error, "code", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict):
            code = code or inner.get("code")
    mapped = error_from_code(code, status)
    if mapped:
        return mapped
    if status == 401:
        return LLMError("ChatGPT rejected the access token — run /login chatgpt", retryable=False, status=401)
    return None


class ChatGPTBackend:
    name = "chatgpt"

    def __init__(self, llm):
        self.model = llm["model"]
        self.timeout = float(llm.get("timeout", 120))
        self.auth = None  # resolved lazily so importing this module stays cheap
        self._usage = None
        self._reasoning = []

    def _auth(self):
        return self.auth or get_chatgpt_auth()

    def _client(self):
        return OpenAI(
            base_url=RESOURCE,
            api_key=self._auth().access_token(),  # refreshes first when needed
            timeout=self.timeout,
            max_retries=0,
        )

    # ---- probing ---------------------------------------------------------

    def list_models(self, timeout=5.0):
        token = self._auth().access_token()
        try:
            response = httpx.get(
                f"{RESOURCE}/models", headers={"Authorization": f"Bearer {token}"}, timeout=timeout
            )
        except httpx.HTTPError as error:
            raise LLMError(f"Could not reach ChatGPT: {error}", retryable=True) from error
        if response.status_code >= 400:
            try:
                err = response.json().get("error", {})
                code = err.get("code") if isinstance(err, dict) else None
            except ValueError:
                code = None
            raise error_from_code(code, response.status_code) or LLMError(
                f"HTTP {response.status_code}", status=response.status_code
            )
        return parse_model_list(response.json())

    def check_model(self, ids, timeout=5.0):
        """No-op: a billed ping would burn plan usage."""

    # ---- chat ------------------------------------------------------------

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        instructions, items = to_responses_input(messages)
        kwargs = {
            "model": self.model,
            "instructions": instructions,
            "input": items,
            "stream": True,
            "store": False,
        }
        if tools:
            kwargs["tools"] = to_responses_tools(tools)
        client = self._client()
        text_parts, tool_calls = [], []
        try:
            stream = client.responses.create(**kwargs)
            self._consume(stream, text_parts, tool_calls, on_text, on_reasoning)
        except LLMError as error:
            error._jarvis_partial = bool(text_parts or tool_calls)
            raise
        except Exception as error:
            mapped = map_api_error(error)
            failure = mapped or error
            try:
                failure._jarvis_partial = bool(text_parts or tool_calls)
            except Exception:
                pass
            if mapped:
                raise mapped from error
            raise
        msg = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        reasoning = "".join(self._reasoning).strip()
        if reasoning:
            msg["reasoning"] = reasoning
        if self._usage:
            msg["usage"] = self._usage
        return msg

    def _consume(self, stream, text_parts, tool_calls, on_text, on_reasoning):
        completed = False
        self._usage = None
        self._reasoning = []
        for event in stream:
            kind = getattr(event, "type", "") or ""
            if kind == "response.output_text.delta":
                text_parts.append(event.delta)
                if on_text:
                    on_text(event.delta)
            elif "reasoning" in kind and kind.endswith(".delta"):
                if getattr(event, "delta", None):
                    self._reasoning.append(event.delta)
                    if on_reasoning:
                        on_reasoning(event.delta)
            elif kind == "response.output_item.done":
                item = event.item
                if getattr(item, "type", "") == "function_call":
                    tool_calls.append({
                        "id": item.call_id,
                        "type": "function",
                        "function": {"name": item.name, "arguments": item.arguments or "{}"},
                    })
            elif kind == "response.completed":
                completed = True
                usage = getattr(getattr(event, "response", None), "usage", None)
                out_tokens = getattr(usage, "output_tokens", None)
                if isinstance(out_tokens, int):
                    inp = getattr(usage, "input_tokens", None)
                    self._usage = {"input": inp if isinstance(inp, int) else None, "output": out_tokens}
            elif kind == "response.failed":
                err = getattr(event.response, "error", None)
                code = getattr(err, "code", None)
                raise error_from_code(code) or LLMError(
                    f"ChatGPT request failed: {getattr(err, 'message', None) or code or 'unknown error'}",
                    retryable=False,
                )
            elif kind == "response.incomplete":
                details = getattr(event.response, "incomplete_details", None)
                reason = getattr(details, "reason", None) or "unknown"
                raise LLMError(f"ChatGPT response incomplete: {reason}", retryable=False)
            elif kind == "error":
                code = getattr(event, "code", None)
                raise error_from_code(code) or LLMError(
                    f"ChatGPT stream error: {getattr(event, 'message', None) or code}", retryable=False
                )
        if not completed:
            raise LLMError("ChatGPT stream ended before response.completed", retryable=True)


def parse_model_list(payload):
    """Model slugs from ChatGPT's {"models": [...]} or the OpenAI {"data": [...]} shape."""
    ids = []
    for entry in payload.get("models") or []:
        if entry.get("visibility") == "list" and entry.get("slug"):
            ids.append(entry["slug"])
    if not ids:
        ids = [m["id"] for m in payload.get("data") or [] if m.get("id")]
    return sorted(ids)
