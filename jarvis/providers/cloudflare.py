"""Cloudflare Workers AI and AI Gateway over the account-scoped REST API.

Both use https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1,
which speaks OpenAI Chat Completions. A ``cf-aig-gateway-id`` header routes a
request through an AI Gateway, which also serves third-party models
("openai/gpt-4.1"). That base URL has no documented /models route, so models
come from Cloudflare's own model search. It only covers Workers AI models;
there is no API listing the third-party ones.

Docs: https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/
      https://developers.cloudflare.com/ai-gateway/usage/rest-api/
"""

import httpx

from .. import provider_fields
from .errors import LLMError, _dig_message
from .openai_chat import ChatBackend

CHAT_TASK = "Text Generation"
_MAX_PAGES = 20

_HINTS = {
    401: " Check the API token and Account ID. The token needs the Workers AI Read permission; "
         "AI Gateway permissions alone are not enough.",
    402: " Add AI Gateway credits, or store your own provider key in the gateway.",
}


class CloudflareBackend(ChatBackend):

    name = "cloudflare"
    hints = _HINTS

    def __init__(self, llm):
        self.provider = llm.get("provider", "cloudflare")
        overrides = llm.get("fields")
        values = provider_fields.values(self.provider, overrides)
        self.missing = provider_fields.missing(self.provider, overrides)
        url = llm["base_url"]
        for key, text in values.items():
            if text:
                url = url.replace(f"{{{key}}}", text)
        headers = {}
        if values.get("gateway_id"):
            headers["cf-aig-gateway-id"] = values["gateway_id"]
        super().__init__({**llm, "base_url": url}, headers=headers)

    def _require_setup(self):
        """Fail with what to do, before any request goes out."""
        if self.missing:
            field = self.missing[0]
            raise LLMError(
                f"Cloudflare needs your {field['label']}. Enter it in Settings, set {field['env']}, "
                f"or run /provider use {self.provider} {field['key']}=VALUE.", retryable=False)
        if self.client.api_key == "not-needed":
            raise LLMError("No Cloudflare API token. Paste one in Settings or set CLOUDFLARE_API_TOKEN.",
                           retryable=False)

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        self._require_setup()
        return super().chat(messages, tools, on_text, on_reasoning)

    def check_model(self, ids, timeout=5.0):
        self._require_setup()
        super().check_model(ids, timeout=timeout)

    def list_models(self, timeout=5.0):
        """Sorted ids of the Workers AI chat models (raises on failure)."""
        self._require_setup()
        root = self.base_url.rstrip("/").removesuffix("/v1")
        found = set()
        # The page size limit is undocumented: read until a page adds nothing new.
        for page in range(1, _MAX_PAGES + 1):
            response = httpx.get(
                f"{root}/models/search",
                params={"task": CHAT_TASK, "page": page, "per_page": 100},
                headers={"Authorization": f"Bearer {self.client.api_key}"},
                timeout=timeout,
            )
            names = _chat_model_names(_result_of(response))
            if names <= found:
                break
            found |= names
        return sorted(found)


def _result_of(response):
    """The "result" list of a Cloudflare API response, or an LLMError."""
    try:
        data = response.json()
    except ValueError:
        data = None
    status = response.status_code
    if status >= 400 or not isinstance(data, dict) or data.get("success") is False:
        message = _dig_message(data) or "unexpected response from the model search"
        # The model search answers a bad token with 400 "Authentication failed", not 401.
        hint = _HINTS.get(401 if status == 400 and "authenticat" in message.lower() else status, "")
        raise LLMError(f"Cloudflare returned HTTP {status}: {message.rstrip('.')}.{hint}",
                       status=status, retryable=status == 429 or status >= 500,
                       details=response.text[:2000])
    return data.get("result") or []


def _chat_model_names(items):
    """Model ids from search results (the item shape is not in the API docs)."""
    names = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        task = item.get("task")
        task = task.get("name") if isinstance(task, dict) else task
        if not task or task == CHAT_TASK:
            names.add(item["name"])
    return names
