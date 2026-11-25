"""Provider backends, retry policy and ChatGPT sign-in (no network)."""

import base64
import copy
import json
import os
import stat
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jarvis.config import DEFAULTS
from jarvis.llm import LLM, LLMError
from jarvis.providers import PRESETS
from jarvis.providers import anthropic_native as an
from jarvis.providers import chatgpt as cg
from jarvis.providers import chatgpt_auth as auth_mod
from jarvis.providers.chatgpt_auth import ChatGPTAuth, pkce_challenge


def make_config(provider="lmstudio", **llm):
    config = copy.deepcopy(DEFAULTS)
    config["llm"].update({"provider": provider, "retries": 2, "retry_delay": 0, "stream": True})
    config["llm"].update(llm)
    return config


# ---- presets ---------------------------------------------------------------

def test_presets_have_backends():
    assert PRESETS["chatgpt"]["backend"] == "chatgpt"
    assert PRESETS["anthropic"]["backend"] == "anthropic"
    assert PRESETS["anthropic"]["default_model"] == "claude-opus-5-5"
    assert all(p["backend"] == "chat" for n, p in PRESETS.items() if n not in {"chatgpt", "anthropic"})
    assert LLM(make_config("custom")).backend == "chat"
    assert LLM(make_config("chatgpt")).backend == "chatgpt"


# ---- chat backend ----------------------------------------------------------

def _chunk(content=None, reasoning=None):
    delta = SimpleNamespace(content=content, tool_calls=[], reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _fake_client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_chat_streams_reasoning_content():
    llm = LLM(make_config())
    llm.client = _fake_client(lambda **kw: iter([_chunk(reasoning="think "), _chunk(reasoning="hard"), _chunk("answer")]))
    seen = []
    msg = llm.chat([{"role": "user", "content": "hi"}], on_reasoning=seen.append)
    assert seen == ["think ", "hard"]
    assert msg["reasoning"] == "think hard"
    assert msg["content"] == "answer"


class StatusError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _counting_create(errors):
    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        if state["n"] <= len(errors):
            raise errors[state["n"] - 1]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))])

    return create, state


def test_no_retry_on_400():
    llm = LLM(make_config(stream=False))
    create, state = _counting_create([StatusError(400), StatusError(400), StatusError(400)])
    llm.client = _fake_client(create)
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "hi"}])
    assert state["n"] == 1


@pytest.mark.parametrize("error", [StatusError(503), StatusError(429), ConnectionError("down")])
def test_retry_on_transient(error):
    llm = LLM(make_config(stream=False))
    create, state = _counting_create([error, error])
    llm.client = _fake_client(create)
    assert llm.chat([{"role": "user", "content": "hi"}])["content"] == "ok"
    assert state["n"] == 3


# ---- ChatGPT Responses backend ---------------------------------------------

HISTORY = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "list files"},
    {"role": "assistant", "content": "ok", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{\"path\": \".\"}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
]


def test_responses_input_conversion():
    instructions, items = cg.to_responses_input(HISTORY)
    assert instructions == "be brief"
    assert items == [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "ok"},
        {"type": "function_call", "call_id": "c1", "name": "list_dir", "arguments": "{\"path\": \".\"}"},
        {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
    ]
    assert not any(item.get("role") == "system" for item in items)
    tools = cg.to_responses_tools([{"type": "function", "function": {"name": "x", "description": "d", "parameters": {"type": "object"}}}])
    assert tools == [{"type": "function", "name": "x", "description": "d", "parameters": {"type": "object"}}]


def _ev(type, **kw):
    return SimpleNamespace(type=type, **kw)


def _chatgpt_llm(events, calls=None):
    llm = LLM(make_config("chatgpt", model="gpt-x"))

    def create(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return iter(events)

    llm._impl._client = lambda: SimpleNamespace(responses=SimpleNamespace(create=create))
    return llm


def test_responses_stream_text_and_function_call():
    calls = []
    item = SimpleNamespace(type="function_call", call_id="c9", name="list_dir", arguments="{}")
    llm = _chatgpt_llm([
        _ev("response.reasoning_summary_text.delta", delta="hmm"),
        _ev("response.output_text.delta", delta="Hel"),
        _ev("response.output_text.delta", delta="lo"),
        _ev("response.output_item.done", item=item),
        _ev("response.completed", response=None),
    ], calls)
    text, reasoning = [], []
    msg = llm.chat(HISTORY, tools=[{"type": "function", "function": {"name": "list_dir"}}],
                   on_text=text.append, on_reasoning=reasoning.append)
    assert msg["content"] == "Hello" and text == ["Hel", "lo"] and reasoning == ["hmm"]
    assert msg["tool_calls"][0]["id"] == "c9"
    sent = calls[0]
    assert sent["store"] is False and sent["stream"] is True
    for banned in ("max_output_tokens", "temperature", "top_p", "metadata", "user", "truncation",
                   "previous_response_id", "background", "prompt"):
        assert banned not in sent


def test_responses_usage_limit_is_not_retried():
    calls = []
    err = SimpleNamespace(code="subscription_sharing_usage_limit_exceeded", message="limit")
    llm = _chatgpt_llm([_ev("response.failed", response=SimpleNamespace(error=err))], calls)
    with pytest.raises(LLMError, match="usage limit"):
        llm.chat(HISTORY)
    assert len(calls) == 1


def test_responses_requires_completed_event():
    llm = _chatgpt_llm([_ev("response.output_text.delta", delta="x")])
    llm.retries = 0
    with pytest.raises(LLMError):
        llm.chat(HISTORY)


def test_parse_model_list():
    payload = {"models": [
        {"slug": "gpt-a", "visibility": "list"}, {"slug": "hidden", "visibility": "hide"}]}
    assert cg.parse_model_list(payload) == ["gpt-a"]
    assert cg.parse_model_list({"data": [{"id": "m1"}]}) == ["m1"]


# ---- Anthropic backend -----------------------------------------------------

def test_thinking_model_rule():
    assert an.supports_thinking("claude-opus-5-5")
    assert an.supports_thinking("claude-opus-4-6")
    assert an.supports_thinking("claude-sonnet-4-6")
    assert an.supports_thinking("claude-haiku-5")
    assert an.supports_thinking("claude-fable-5-2")
    assert not an.supports_thinking("claude-haiku-4-5")
    assert not an.supports_thinking("claude-3-5-sonnet-20241022")
    assert not an.supports_thinking("claude-opus-4-1-20250805")
    assert not an.supports_thinking("claude-opus-4-20250514")


def test_anthropic_message_conversion():
    history = [
        {"role": "system", "content": "sys one"},
        {"role": "system", "content": "sys two"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{\"x\": 1}"}},
            {"id": "t2", "type": "function", "function": {"name": "b", "arguments": "not json"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": ""},  # nothing to send: skipped
    ]
    system, msgs = an.to_anthropic_messages(history, "claude-opus-5-5")
    assert system == "sys one\n\nsys two"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "a", "input": {"x": 1}}
    assert msgs[1]["content"][1]["input"] == {}
    last = msgs[2]["content"]
    assert [b["type"] for b in last] == ["tool_result", "tool_result", "text"]
    assert not any(b.get("type") == "text" and not b["text"] for m in msgs for b in m["content"])


def test_anthropic_provider_state_replay_only_when_matching():
    raw = [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "hi"}]
    msg = {"role": "assistant", "content": "hi",
           "provider_state": {"backend": "anthropic", "model": "claude-opus-5-5", "items": raw}}
    _, same = an.to_anthropic_messages([{"role": "user", "content": "q"}, msg], "claude-opus-5-5")
    assert same[1]["content"] == raw
    _, other = an.to_anthropic_messages([{"role": "user", "content": "q"}, msg], "claude-sonnet-5-5")
    assert other[1]["content"] == [{"type": "text", "text": "hi"}]


class Block(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return dict(vars(self))


def test_anthropic_final_message():
    final = SimpleNamespace(stop_reason="max_tokens", content=[
        Block(type="thinking", thinking="mull", signature="s"),
        Block(type="text", text="Hi"),
        Block(type="tool_use", id="t1", name="a", input={"k": 1}),
    ])
    msg = an.final_to_message(final, "claude-opus-5-5")
    assert msg["content"].startswith("Hi") and msg["content"].endswith("[response truncated]")
    assert msg["reasoning"] == "mull"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"k": 1}
    assert msg["provider_state"]["backend"] == "anthropic"
    assert msg["provider_state"]["items"][0]["type"] == "thinking"


def test_anthropic_refusal():
    final = SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"), content=None)
    msg = an.final_to_message(final, "claude-opus-5-5")
    assert "declined" in msg["content"] and "cyber" in msg["content"]
    assert "provider_state" not in msg


def test_anthropic_request_shape():
    backend = an.AnthropicBackend(make_config("anthropic", model="claude-opus-5-5", effort="high", max_tokens=100)["llm"])
    req = backend.build_request([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}])
    assert req["max_tokens"] == 32000 and req["fallbacks"] == "default"
    assert set(req["betas"]) == {an.THINKING_BETA, an.FALLBACK_BETA}
    assert req["output_config"] == {"effort": "high"}
    assert req["system"] == [{"type": "text", "text": "s"}]
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in req
    old = an.AnthropicBackend(make_config("anthropic", model="claude-haiku-4-5")["llm"])
    req = old.build_request([{"role": "user", "content": "q"}])
    assert "thinking" not in req and "betas" not in req and "fallbacks" not in req


# ---- Sign in with ChatGPT --------------------------------------------------

def _jwt(claims):
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class FakeOpenAIAuth:
    """httpx.MockTransport handler standing in for auth.openai.com."""

    def __init__(self):
        self.requests = []
        self.next = None  # callable(form) -> httpx.Response

    def __call__(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((request.url.path, form))
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"revocation_endpoint": "https://auth.openai.com/revoke"})
        return self.next(form)


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    fake = FakeOpenAIAuth()
    instance = ChatGPTAuth(transport=httpx.MockTransport(fake))
    instance.fake = fake
    return instance


def test_pkce_challenge_matches_rfc7636_example():
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _login(auth, client_id="oaiapp_one", scope=auth_mod.SCOPE, email="me@example.com"):
    attempt = auth._new_attempt(auth._load())

    def respond(form):
        id_token = _jwt({"iss": auth_mod.ISSUER, "aud": form["client_id"], "exp": time.time() + 600,
                         "nonce": attempt["nonce"], "sub": "user-1", "email": email})
        return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1",
                                         "expires_in": 3600, "scope": scope, "id_token": id_token})

    auth.fake.next = respond
    auth.complete_login(attempt, {"state": attempt["state"], "code": "abc", "client_id": client_id})
    return attempt


def test_authorize_url_first_vs_reauth(auth):
    first = auth._new_attempt(auth._load())
    q = {k: v[0] for k, v in parse_qs(urlparse(first["auth_url"]).query).items()}
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "Jarvis"
    assert q["ext_agent_host_id"].startswith("urn:uuid:")
    assert q["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert q["code_challenge"] == pkce_challenge(first["verifier"]) and q["code_challenge_method"] == "S256"
    assert q["resource"] == "https://api.openai.com/v1" and "chatgpt.tokens.use.direct" in q["scope"]
    assert "id_token_hint" not in q and "login_hint" not in q

    _login(auth)
    data = auth._load()
    assert data["client_id"] == "oaiapp_one"  # issued id saved, never the placeholder
    second = auth._new_attempt(data)
    q2 = {k: v[0] for k, v in parse_qs(urlparse(second["auth_url"]).query).items()}
    assert q2["client_id"] == "oaiapp_one" and "agent_name_hint" not in q2
    assert q2["login_hint"] == "me@example.com" and q2["id_token_hint"]
    assert q2["ext_agent_host_id"] == q["ext_agent_host_id"]  # stable per host
    assert q2["state"] != q["state"] and q2["nonce"] != q["nonce"]


def test_callback_validation(auth):
    attempt = auth._new_attempt(auth._load())
    with pytest.raises(LLMError, match="state"):
        auth.complete_login(attempt, {"state": "wrong", "code": "c", "client_id": "oaiapp_x"})
    with pytest.raises(LLMError, match="cancelled"):
        auth.complete_login(attempt, {"state": attempt["state"], "error": "access_denied"})
    with pytest.raises(LLMError, match="client_id"):
        auth.complete_login(attempt, {"state": attempt["state"], "code": "c"})
    assert auth.fake.requests == []  # nothing reached the token endpoint

    _login(auth)
    again = auth._new_attempt(auth._load())
    with pytest.raises(LLMError, match="client_id changed"):
        auth.complete_login(again, {"state": again["state"], "code": "c", "client_id": "oaiapp_other"})


def test_login_storage_is_private_and_status(auth):
    _login(auth)
    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
    assert mode(auth.path) == 0o600 and mode(auth.path.parent) == 0o700
    assert [p.name for p in auth.path.parent.iterdir()] == ["chatgpt_auth.json"]  # no temp leftovers
    st = auth.status()
    assert st["signed_in"] and st["plan_ok"] and st["email"] == "me@example.com"
    assert auth.access_token() == "AT1"
    path, form = auth.fake.requests[0]
    assert form["grant_type"] == "authorization_code" and form["resource"] == auth_mod.RESOURCE
    assert form["redirect_uri"] == auth_mod.REDIRECT_URI and form["client_id"] == "oaiapp_one"


def test_missing_scope_means_plan_not_ok(auth):
    _login(auth, scope="openid offline_access")
    st = auth.status()
    assert st["signed_in"] and not st["plan_ok"] and st["error"]
    with pytest.raises(LLMError):
        auth.access_token()


def test_refresh_rotates_refresh_token(auth):
    _login(auth)
    data = auth._load()
    data["tokens"]["expires_at"] = time.time() + 30  # inside the 120 s margin
    auth._save(data)
    auth.fake.next = lambda form: httpx.Response(200, json={
        "access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600, "scope": auth_mod.SCOPE})
    assert auth.access_token() == "AT2"
    path, form = auth.fake.requests[-1]
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "RT1" and "scope" not in form
    tokens = auth._load()["tokens"]
    assert tokens["refresh_token"] == "RT2" and tokens["access_token"] == "AT2"
    assert tokens["expires_at"] > time.time() + 3000
    n = len(auth.fake.requests)
    assert auth.access_token() == "AT2" and len(auth.fake.requests) == n  # still fresh: no refresh


def test_terminal_refresh_error_clears_tokens(auth):
    _login(auth)
    data = auth._load()
    data["tokens"]["expires_at"] = 0
    auth._save(data)
    auth.fake.next = lambda form: httpx.Response(400, json={"error": "refresh_token_reused"})
    with pytest.raises(LLMError, match="run /login chatgpt"):
        auth.access_token()
    after = auth._load()
    assert "tokens" not in after and after["client_id"] == "oaiapp_one" and after["host_id"]
    assert not auth.status()["signed_in"]


def test_logout_revokes_and_keeps_client_id(auth):
    _login(auth)
    auth.fake.next = lambda form: httpx.Response(200, json={})
    auth.logout()
    after = auth._load()
    assert "tokens" not in after and after["client_id"] == "oaiapp_one"
    assert not auth.status()["signed_in"]


def test_callback_page_reports_failure_instead_of_success(auth, monkeypatch):
    """The browser tab must not say "Signed in" when the sign-in failed."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(auth_mod, "REDIRECT_PORT", port)
    auth.start_login(open_browser=False)
    state = auth._attempt["state"]
    page = httpx.get(
        f"http://127.0.0.1:{port}{auth_mod.REDIRECT_PATH}",
        params={"state": state, "error": "unsupported_plan", "error_description": "not eligible"},
        timeout=10,
    ).text
    assert "Sign-in failed" in page and "Signed in" not in page
    assert "unsupported_plan" in page and "not eligible" in page
    st = auth.wait(timeout=10)
    assert not st["signed_in"] and not st["pending"] and "unsupported_plan" in st["error"]


def test_gemini_models_prefix_is_stripped():
    from types import SimpleNamespace
    from jarvis.providers.openai_chat import ChatBackend
    config = {"base_url": "http://x/v1", "model": "models/gemini-2.5-flash", "stream": False,
              "max_tokens": 10, "api_key": "k"}
    backend = ChatBackend(config)
    assert backend.model == "gemini-2.5-flash"
    backend.client = SimpleNamespace(models=SimpleNamespace(list=lambda timeout: SimpleNamespace(
        data=[SimpleNamespace(id="models/gemini-2.5-pro"), SimpleNamespace(id="models/gemini-2.5-flash")])))
    assert backend.list_models() == ["gemini-2.5-flash", "gemini-2.5-pro"]
