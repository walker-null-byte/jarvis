"""Cloudflare Workers AI / AI Gateway against a local fake of the documented API."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis import provider_fields
from jarvis.config import DEFAULTS
from jarvis.llm import LLM, LLMError
from jarvis.providers import PRESETS

PAGES = [
    [{"name": "@cf/zai-org/glm-4.7-flash", "task": {"name": "Text Generation"}},
     {"name": "@cf/baai/bge-m3", "task": {"name": "Text Embeddings"}}],
    [{"name": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "task": {"name": "Text Generation"}}],
]


class FakeCloudflare(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, kind="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body=None):
        self.server.seen.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
        if self.headers.get("Authorization") == "Bearer good-token":
            return True
        self._send(401, {"success": False, "result": None,
                         "errors": [{"code": 10000, "message": "Authentication error"}]})
        return False

    def do_GET(self):
        if self._record():
            page = int(parse_qs(urlparse(self.path).query)["page"][0])
            result = PAGES[page - 1] if page <= len(PAGES) else []
            self._send(200, {"success": True, "errors": [], "messages": [], "result": result})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self._record(body):
            return
        deltas = [{"content": "Checking."},
                  {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                   "function": {"name": "list_dir", "arguments": '{"path":'}}]},
                  {"tool_calls": [{"index": 0, "function": {"arguments": ' "."}'}}]}]
        chunks = [{"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                   "choices": [{"index": 0, "delta": d}]} for d in deltas]
        stream = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        self._send(200, stream, "text/event-stream")


@pytest.fixture
def server(monkeypatch):
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_GATEWAY_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloudflare)
    httpd.seen = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def make_llm(server, provider="cloudflare", token="good-token", **extra):
    config = copy.deepcopy(DEFAULTS)
    path = PRESETS[provider]["base_url"].removeprefix("https://api.cloudflare.com")
    config["llm"].update(provider=provider, api_key=token, retries=0, stream=True,
                         model="@cf/zai-org/glm-4.7-flash",
                         base_url=f"http://127.0.0.1:{server.server_port}{path}", **extra)
    return LLM(config)


def test_workers_ai_lists_chat_models_across_pages(server):
    provider_fields.save("cloudflare", {"account_id": "acc123"})
    llm = make_llm(server)
    ok, message, ids, _ = llm.probe(check_model=False)
    assert ok, message
    assert ids == ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/zai-org/glm-4.7-flash"]
    first = server.seen[0]
    assert first["path"].startswith("/client/v4/accounts/acc123/ai/models/search?")
    assert parse_qs(urlparse(first["path"]).query)["task"] == ["Text Generation"]
    assert "cf-aig-gateway-id" not in {k.lower() for k in first["headers"]}


@pytest.mark.parametrize("saved, sent", [({}, "default"), ({"gateway_id": "my-gw"}, "my-gw")])
def test_gateway_streams_tool_calls_with_gateway_header(server, saved, sent):
    provider_fields.save("cloudflare-gateway", {"account_id": "acc123", **saved})
    llm = make_llm(server, "cloudflare-gateway")
    msg = llm.chat([{"role": "user", "content": "ls"}], tools=[
        {"type": "function", "function": {"name": "list_dir", "parameters": {"type": "object"}}}])
    assert msg["content"] == "Checking."
    assert msg["tool_calls"][0]["function"] == {"name": "list_dir", "arguments": '{"path": "."}'}
    request = server.seen[-1]
    assert request["path"] == "/client/v4/accounts/acc123/ai/v1/chat/completions"
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["cf-aig-gateway-id"] == sent
    assert headers["authorization"] == "Bearer good-token"


def test_bad_token_gets_cloudflare_message_and_permission_hint(server):
    provider_fields.save("cloudflare", {"account_id": "acc123"})
    llm = make_llm(server, token="wrong")
    with pytest.raises(LLMError) as error:
        llm.chat([{"role": "user", "content": "hi"}])
    assert "Authentication error" in str(error.value) and "Workers AI Read" in str(error.value)
    assert "Check the API key." not in str(error.value)
    ok, message, _, status = llm.probe()
    assert not ok and status == 401 and "Authentication error" in message


def test_missing_setup_is_explained_without_any_request(server):
    llm = make_llm(server)
    ok, message, _, _ = llm.probe()
    assert not ok and "Account ID" in message and "CLOUDFLARE_ACCOUNT_ID" in message
    with pytest.raises(LLMError, match="Account ID"):
        llm.chat([{"role": "user", "content": "hi"}])
    assert server.seen == []


def test_missing_token_is_explained(server):
    provider_fields.save("cloudflare", {"account_id": "acc123"})
    llm = make_llm(server, token=None, api_key_env="CLOUDFLARE_API_TOKEN")
    ok, message, _, _ = llm.probe()
    assert not ok and "CLOUDFLARE_API_TOKEN" in message and server.seen == []


def test_field_values_order_and_validation(monkeypatch, server):
    gateway = PRESETS["cloudflare-gateway"]["fields"][1]
    assert provider_fields.value(gateway) == "default"
    monkeypatch.setenv("CLOUDFLARE_GATEWAY_ID", "from-env")
    assert provider_fields.value(gateway) == "from-env"
    provider_fields.save("cloudflare-gateway", {"gateway_id": "saved"})
    assert provider_fields.value(gateway) == "saved"
    assert provider_fields.value(gateway, {"gateway_id": "typed"}) == "typed"
    provider_fields.save("cloudflare-gateway", {"gateway_id": ""})
    assert provider_fields.value(gateway) == "from-env"
    for bad in ({"account_id": "../../zones"}, {"account_id": "a b"}, {"nope": "x"}):
        with pytest.raises(ValueError):
            provider_fields.save("cloudflare", bad)
