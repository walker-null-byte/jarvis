"""OpenAI-compatible LLM client with streaming and tool-call support.

Works against LM Studio, Ollama, llama.cpp, vLLM, or Gemini's OpenAI endpoint —
whatever config.toml points at.
"""

import os

from openai import OpenAI


class LLM:
    def __init__(self, config):
        llm = config["llm"]
        api_key = os.environ.get(llm["api_key_env"]) or "not-needed"
        self.client = OpenAI(base_url=llm["base_url"], api_key=api_key)
        self.model = llm["model"]
        self.stream = llm["stream"]
        self.max_tokens = llm["max_tokens"]

    def chat(self, messages, tools=None, on_text=None):
        """Send one chat request; returns an assistant message dict.

        The returned dict is API-ready for the next request:
        {"role": "assistant", "content": str, "tool_calls": [...]?}

        on_text(token) is called for each streamed text fragment.
        """
        kwargs = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if tools:
            kwargs["tools"] = tools

        if not self.stream:
            return self._chat_blocking(kwargs, on_text)
        return self._chat_streaming(kwargs, on_text)

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

        msg = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            # Some local servers omit tool-call IDs; synthesize them.
            for i in sorted(tool_calls):
                if not tool_calls[i]["id"]:
                    tool_calls[i]["id"] = f"call_{i}"
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return msg
