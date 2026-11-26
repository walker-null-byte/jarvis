"""OpenAI-compatible Chat Completions backend (LM Studio, Ollama, vLLM, ...)."""

import os
import re

from openai import OpenAI

from . import effort as effort_mod
from .thinking import ThinkSplitter, split_all


def resolve_key(llm):
    """Literal key, then a key saved in the keystore, then the env var, then "not-needed"."""
    from .. import keystore
    name = llm.get("api_key_env") or ""
    return (llm.get("api_key") or (keystore.get(name) if name else None)
            or os.environ.get(name) or "not-needed")


def _bare_id(model_id):
    """Gemini lists ids as "models/gemini-..."; the chat call wants the bare id."""
    return model_id.removeprefix("models/") if isinstance(model_id, str) else model_id


# Gemini 3+ requires a thought signature on function calls it did not produce itself.
# Documented dummy values (ai.google.dev "Thought signatures" FAQ 1).
GEMINI_DUMMY_SIGNATURE = "skip_thought_signature_validator"

_MESSAGE_KEYS = ("role", "content", "name", "tool_call_id", "tool_calls")
_OPTIONAL_HINTS = {
    "stream_options": re.compile(r"stream_options|include_usage|stream option", re.I),
    "reasoning_effort": re.compile(r"reasoning_effort|reasoning effort", re.I),
    "extra_body": re.compile(r"thinking|include_thoughts|extra_body|\bgoogle\b|\breasoning\b(?!_effort)", re.I),
}
_TOKEN_HINT = re.compile(r"max_tokens|max_completion_tokens|maximum|output tokens|completion tokens", re.I)
_CONTEXT_HINT = re.compile(r"context (length|window)|context_length|input tokens|prompt", re.I)
_CAP_NUMBER = re.compile(
    r"(?:at most|no more than|up to|maximum(?: value| allowed| of| is| output)?|limit(?: is| of)?|<=|"
    r"less than or equal to|between 1 and|range)\D{0,40}?(\d[\d,]{2,})", re.I)
FALLBACK_CAP = 4096


def parse_token_cap(text, requested):
    """The output limit a server names in a max_tokens rejection, else FALLBACK_CAP."""
    for match in _CAP_NUMBER.finditer(text or ""):
        value = int(match.group(1).replace(",", ""))
        if 0 < value < requested:
            return value
    return min(FALLBACK_CAP, max(1, requested - 1))


def stop_reason(raw):
    """Normalise a Chat Completions finish_reason (None when the server sent none)."""
    if not raw:
        return None
    return {"stop": "end", "function_call": "tool_calls"}.get(raw, raw)
_VAGUE_REJECTION = re.compile(
    r"additional propert|unrecogni[sz]ed|unknown (field|parameter|argument|name)|"
    r"unsupported parameter|extra inputs|not permitted|unexpected (field|keyword)", re.I)


def is_gemini(base_url):
    return "generativelanguage.googleapis.com" in str(base_url or "")


def _extra_of(obj, key="extra_content"):
    """An undeclared field on an SDK object (kept by pydantic as an extra) or dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    value = getattr(obj, key, None)
    if value is None:
        extras = getattr(obj, "model_extra", None)
        if isinstance(extras, dict):
            value = extras.get(key)
    if value is not None and hasattr(value, "model_dump"):
        value = value.model_dump()
    return value if isinstance(value, dict) else None


def _usage_of(usage):
    """{"input": n, "output": n} from an SDK usage object, or None."""
    if usage is None:
        return None
    out = getattr(usage, "completion_tokens", None)
    if not isinstance(out, int):
        return None
    inp = getattr(usage, "prompt_tokens", None)
    return {"input": inp if isinstance(inp, int) else None, "output": out}


def clean_messages(messages, gemini=False, echo_reasoning=False):
    """Messages as the Chat Completions API wants them.

    Jarvis keeps extra keys on history messages (reasoning, stats, provider
    state); strict servers reject unknown fields, so only the standard ones go
    out. Gemini's thought-signature ``extra_content`` on tool calls is kept
    (and filled with the documented dummy when missing); other servers never see it.

    echo_reasoning (DeepSeek thinking mode): an assistant message that made tool calls in
    the *current* turn (after the last user message) is sent back with its reasoning as
    ``reasoning_content``, which the API requires; earlier turns never carry it.
    """
    out = []
    turn_start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    for index, msg in enumerate(messages):
        clean = {k: msg[k] for k in _MESSAGE_KEYS if k in msg and msg[k] is not None}
        calls = msg.get("tool_calls")
        if echo_reasoning and calls and index > turn_start and msg.get("reasoning"):
            clean["reasoning_content"] = msg["reasoning"]
        if calls:
            cleaned = []
            for call in calls:
                item = {k: call[k] for k in ("id", "type", "function") if k in call}
                extra = call.get("extra_content")
                if gemini and extra:
                    item["extra_content"] = extra
                cleaned.append(item)
            if gemini and not any(c.get("extra_content") for c in cleaned):
                # History from another provider (or an old session): the first call of a
                # step must carry a signature, so give it the documented dummy.
                cleaned[0]["extra_content"] = {"google": {"thought_signature": GEMINI_DUMMY_SIGNATURE}}
            clean["tool_calls"] = cleaned
        out.append(clean)
    return out


class ChatBackend:
    """Streams or blocks one Chat Completions request; no retry logic here."""

    name = "chat"

    def __init__(self, llm, headers=None):
        self.base_url = llm["base_url"]
        self.model = _bare_id(llm["model"])
        self.stream = llm["stream"]
        self.max_tokens = llm["max_tokens"]
        self.gemini = is_gemini(self.base_url)
        self.deepseek = llm.get("provider") == "deepseek"
        self.dropped = set()  # optional request fields this server rejected
        self.cap = None       # output limit this server insisted on (see _create)
        self.token_key = "max_tokens"
        self.provider = llm.get("provider", "custom")
        self.effort = effort_mod.normalize(llm.get("effort"))
        self.client = OpenAI(
            base_url=llm["base_url"],
            api_key=resolve_key(llm),
            timeout=float(llm.get("timeout", 120)),
            max_retries=0,
            default_headers=headers or None,
        )

    def list_models(self, timeout=5.0):
        """Sorted model ids the endpoint exposes (raises on failure)."""
        page = self.client.models.list(timeout=timeout)
        return sorted(_bare_id(model.id) for model in page.data)

    def check_model(self, ids, timeout=5.0):
        """Send a 1-token request so a bad model/key fails now, not mid-turn."""
        self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            stream=False,
            timeout=timeout,
        )

    def effective_max_tokens(self, override=None):
        limit = int(override or self.max_tokens)
        return min(limit, self.cap) if self.cap else limit

    def _request(self, messages, tools, max_tokens=None):
        kwargs = {"model": self.model, "messages": clean_messages(messages, self.gemini, self.deepseek),
                  self.token_key: self.effective_max_tokens(max_tokens)}
        if tools:
            kwargs["tools"] = tools
        if self.stream and "stream_options" not in self.dropped:
            kwargs["stream_options"] = {"include_usage": True}
        top, body = effort_mod.chat_fields(self.provider, self.base_url, self.model, self.effort)
        if "reasoning_effort" in top and "reasoning_effort" not in self.dropped:
            kwargs["reasoning_effort"] = top["reasoning_effort"]
        if self.gemini and self.effort != "off":
            # Thought summaries; they come back inline as <thought>...</thought>.
            body = {**body, "extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}}
        if body and "extra_body" not in self.dropped:
            kwargs["extra_body"] = body
        return kwargs

    def _create(self, kwargs, **extra):
        """create(), retrying when the server rejects an optional field or the output limit.

        Some OpenAI-compatible servers refuse ``stream_options``, ``reasoning_effort`` or
        Gemini's thinking config with a 400/422; others cap max_tokens below what was asked
        (or want ``max_completion_tokens``). Each fix is applied once, then remembered for
        the lifetime of this client so only the first request pays for the retry.
        """
        fixed = set()
        while True:
            try:
                return self.client.chat.completions.create(**kwargs, **extra)
            except Exception as error:
                if getattr(error, "status_code", None) not in (400, 422):
                    raise
                text = f"{error} {getattr(error, 'body', '')}"
                kwargs = self._fix_request(kwargs, text, fixed)
                if kwargs is None:
                    raise

    def _fix_request(self, kwargs, text, fixed):
        """kwargs adjusted for what the rejection says, or None when it is not fixable."""
        key = self.token_key
        if key in kwargs and "tokens" not in fixed and _TOKEN_HINT.search(text):
            asked = kwargs[key]
            if key == "max_tokens" and "max_completion_tokens" in text and re.search(
                    r"unsupported|not supported|instead|use max_completion", text, re.I):
                self.token_key = "max_completion_tokens"
                fixed.add("tokens")
                return {("max_completion_tokens" if k == "max_tokens" else k): v for k, v in kwargs.items()}
            if isinstance(asked, int) and asked > 1 and not (
                    _CONTEXT_HINT.search(text) and not re.search(r"max_(completion_)?tokens", text)):
                self.cap = parse_token_cap(text, asked)
                fixed.add("tokens")
                return {**kwargs, key: self.cap}
        present = [k for k in _OPTIONAL_HINTS if k in kwargs]
        named = [k for k in present if _OPTIONAL_HINTS[k].search(text)]
        if not named and present and _VAGUE_REJECTION.search(text):
            named = present
        if not named or set(named) <= fixed:
            return None
        self.dropped.update(named)
        fixed.update(named)
        return {k: v for k, v in kwargs.items() if k not in named}

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None, max_tokens=None):
        kwargs = self._request(messages, tools, max_tokens)
        if not self.stream:
            return self._blocking(kwargs, on_text, on_reasoning)
        return self._streaming(kwargs, on_text, on_reasoning)

    def _blocking(self, kwargs, on_text, on_reasoning):
        response = self._create(kwargs)
        choice = response.choices[0].message
        stop = stop_reason(getattr(response.choices[0], "finish_reason", None))
        inline, content = split_all(choice.content or "")
        reasoning = (_reasoning_of(choice) + inline).strip()
        msg = {"role": "assistant", "content": content}
        if reasoning:
            msg["reasoning"] = reasoning
            if on_reasoning:
                on_reasoning(reasoning)
        if choice.tool_calls:
            calls = []
            for i, tc in enumerate(choice.tool_calls):
                call = {
                    "id": tc.id or f"call_{i}",
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments or ""},
                }
                extra = _extra_of(tc)
                if extra:
                    call["extra_content"] = extra
                calls.append(call)
            msg["tool_calls"] = calls
        if stop:
            msg["stop"] = stop
        usage = _usage_of(getattr(response, "usage", None))
        if usage:
            msg["usage"] = usage
        if on_text and msg["content"]:
            on_text(msg["content"])
        return msg

    def _streaming(self, kwargs, on_text, on_reasoning):
        text_parts = []
        reasoning_parts = []
        tool_calls = {}  # index -> accumulating call dict
        usage = None
        stop = None
        splitter = ThinkSplitter()

        def emit(pieces):
            for kind, piece in pieces:
                if kind == "reasoning":
                    reasoning_parts.append(piece)
                    if on_reasoning:
                        on_reasoning(piece)
                else:
                    text_parts.append(piece)
                    if on_text:
                        on_text(piece)

        try:
            for chunk in self._create(kwargs, stream=True):
                usage = _usage_of(getattr(chunk, "usage", None)) or usage
                if not chunk.choices:
                    continue
                stop = stop_reason(getattr(chunk.choices[0], "finish_reason", None)) or stop
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                reasoning = _reasoning_of(delta)
                if reasoning:
                    emit([("reasoning", reasoning)])
                if delta.content:
                    emit(splitter.feed(delta.content))
                for tc in delta.tool_calls or []:
                    idx = tc.index if tc.index is not None else len(tool_calls)
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.id:
                        slot["id"] = tc.id
                    extra = _extra_of(tc)
                    if extra:  # Gemini thought signature: keep it verbatim
                        slot["extra_content"] = extra
                    if tc.function:
                        if tc.function.name:
                            slot["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["function"]["arguments"] += tc.function.arguments
            emit(splitter.finish())
        except Exception as error:
            try:
                error._jarvis_partial = bool(text_parts or reasoning_parts or tool_calls)  # type: ignore[attr-defined]
            except Exception:
                pass
            raise

        msg = {"role": "assistant", "content": "".join(text_parts)}
        reasoning = "".join(reasoning_parts).strip()
        if reasoning:
            msg["reasoning"] = reasoning
        if tool_calls:
            # Some local servers omit tool-call IDs; synthesize them.
            for i in sorted(tool_calls):
                if not tool_calls[i]["id"]:
                    tool_calls[i]["id"] = f"call_{i}"
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        if stop:
            msg["stop"] = stop
        if usage:
            msg["usage"] = usage
        return msg


def _reasoning_of(obj):
    """Reasoning text from a delta/message.

    LM Studio, DeepSeek and vLLM use ``reasoning_content``; OpenRouter uses
    ``reasoning``. The SDK keeps unknown fields as extras, so getattr works.
    """
    return getattr(obj, "reasoning_content", None) or getattr(obj, "reasoning", None) or ""
