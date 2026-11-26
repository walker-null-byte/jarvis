"""Tests for the local web UI server (no network, fake LLM)."""

import asyncio
import base64
import copy
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from jarvis.agent import Jarvis
from jarvis.config import DEFAULTS
from jarvis.events import Event, EventBus
from jarvis.llm import LLM
from jarvis.webui.history import render_history
from jarvis.webui.hub import MAX_QUEUE, EventHub, event_message, sse_format
from jarvis.webui.server import check_bind, create_app

TOKEN = "test-token-123"
BASE = "http://127.0.0.1:7777"


def _config(tmp_path):
    config = copy.deepcopy(DEFAULTS)
    config["paths"]["mind"] = str(tmp_path / "mind")
    config["paths"]["models"] = str(tmp_path / "models")
    config["voice"]["enabled"] = False
    return config


class Env:
    """A real Jarvis + app + authenticated client, with a scriptable fake LLM."""

    def __init__(self, tmp_path, monkeypatch):
        self.gate = None  # set to a threading.Event to make the fake LLM block

        def fake_chat(llm_self, messages, tools=None, on_text=None, **kwargs):
            if self.gate is not None:
                self.gate.wait(5)
            if on_text:
                on_text("Hello from fake")
            return {"role": "assistant", "content": "Hello from fake"}

        monkeypatch.setattr(LLM, "chat", fake_chat)
        self.jarvis = Jarvis(_config(tmp_path))
        self.app = create_app(self.jarvis, TOKEN, host="127.0.0.1", port=7777)
        self.state = self.app.state.web
        self.client = TestClient(self.app, base_url=BASE)
        self.client.headers["Authorization"] = f"Bearer {TOKEN}"

    def wait_idle(self, timeout=5):
        end = time.time() + timeout
        while self.state.busy and time.time() < end:
            time.sleep(0.02)
        assert not self.state.busy


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ---------- security ----------

def test_api_requires_token(env):
    anonymous = TestClient(env.app, base_url=BASE)
    assert anonymous.get("/api/status").status_code == 401
    bad = TestClient(env.app, base_url=BASE, headers={"Authorization": "Bearer nope"})
    assert bad.get("/api/status").status_code == 401
    assert env.client.get("/api/status").status_code == 200


def test_token_query_sets_cookie_and_redirects(env):
    client = TestClient(env.app, base_url=BASE, follow_redirects=False)
    response = client.get(f"/?token={TOKEN}")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/status").status_code == 200  # cookie now carries auth
    assert client.get("/?token=wrong").status_code == 401


def test_bad_host_rejected(env):
    client = TestClient(env.app, base_url="http://evil.example:7777",
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert client.get("/api/status").status_code == 400


def test_foreign_origin_rejected_on_post(env):
    response = env.client.post("/api/cancel", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    ok = env.client.post("/api/cancel", headers={"Origin": BASE})
    assert ok.status_code == 200


def test_security_headers_and_index(env):
    response = env.client.get("/")
    assert response.status_code == 200
    assert "<html" in response.text.lower()
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert env.client.get("/static/js/app.js").status_code == 200


def test_remote_bind_needs_flag():
    assert check_bind("127.0.0.1", False) == ""
    with pytest.raises(ValueError):
        check_bind("0.0.0.0", False)
    assert "WARNING" in check_bind("0.0.0.0", True)


# ---------- chat ----------

def test_chat_runs_turn_and_persists_history(env):
    response = env.client.post("/api/chat", json={"text": "hi there"})
    assert response.status_code == 202
    env.wait_idle()
    stored = env.jarvis.sessions.load(env.jarvis.session_id)
    assert [m["role"] for m in stored] == ["user", "assistant"]
    snapshot = env.client.get("/api/snapshot").json()
    assert [item["type"] for item in snapshot["history"]] == ["user", "assistant"]
    assert snapshot["busy"] is False
    assert [item["index"] for item in snapshot["history"]] == [0, 1]
    assert snapshot["onboarded"] is False
    assert "auto_speak" in snapshot["voice"]


def test_slash_command_returns_output(env):
    response = env.client.post("/api/chat", json={"text": "/help"})
    assert response.status_code == 200
    assert "Slash commands" in response.json()["command_output"]
    assert env.jarvis.history == []  # not sent to the model


def test_busy_returns_409_and_retry(env):
    env.gate = threading.Event()
    assert env.client.post("/api/chat", json={"text": "first"}).status_code == 202
    assert env.client.post("/api/chat", json={"text": "second"}).status_code == 409
    assert env.client.post("/api/retry").status_code == 409
    assert env.client.get("/api/status").json()["busy"] is True
    env.gate.set()
    env.wait_idle()
    assert env.client.post("/api/retry").status_code == 202
    env.wait_idle()
    users = [m for m in env.jarvis.history if m["role"] == "user"]
    assert len(users) == 2


def test_empty_chat_rejected(env):
    assert env.client.post("/api/chat", json={"text": "   "}).status_code == 400


def test_approval_flow(env):
    seen = []
    env.jarvis.events.subscribe(lambda event: seen.append(event))
    result = {}
    thread = threading.Thread(
        target=lambda: result.update(ok=env.jarvis.approvals.request("run_command", {"command": "ls"})))
    thread.start()
    end = time.time() + 3
    while not any(e.kind == "approval_requested" for e in seen) and time.time() < end:
        time.sleep(0.02)
    request_id = next(e.data["request_id"] for e in seen if e.kind == "approval_requested")
    assert env.client.post("/api/approvals/nope", json={"approved": True}).status_code == 404
    assert env.client.post(f"/api/approvals/{request_id}", json={"approved": True}).status_code == 200
    thread.join(3)
    assert result["ok"] is True


# ---------- sessions ----------

def test_session_lifecycle(env):
    env.client.post("/api/chat", json={"text": "remember the milk"})
    env.wait_idle()
    first = env.jarvis.session_id
    new = env.client.post("/api/sessions").json()
    assert new["session"]["id"] != first and new["history"] == []

    listing = env.client.get("/api/sessions").json()
    assert {s["id"] for s in listing["sessions"]} >= {first, new["session"]["id"]}
    assert [s["id"] for s in env.client.get("/api/sessions?q=milk").json()["sessions"]] == [first]

    resumed = env.client.post(f"/api/sessions/{first}/resume").json()
    assert resumed["history"][0]["content"] == "remember the milk"
    assert env.client.patch(f"/api/sessions/{first}", json={"title": "Groceries"}).json()["title"] == "Groceries"

    export = env.client.get(f"/api/sessions/{first}/export?format=markdown")
    assert "attachment" in export.headers["content-disposition"] and "remember the milk" in export.text
    assert env.client.get(f"/api/sessions/{first}/export?format=json").json()["session"]["id"] == first
    assert env.client.get(f"/api/sessions/{first}/export?format=xml").status_code == 400

    assert env.client.post("/api/sessions/does-not-exist/resume").status_code == 404
    assert env.client.delete(f"/api/sessions/{new['session']['id']}").status_code == 200
    assert env.client.get("/api/sessions/..%2f..%2fetc/export").status_code in (400, 404)


def test_cannot_delete_active_session_while_busy(env):
    env.gate = threading.Event()
    env.client.post("/api/chat", json={"text": "go"})
    assert env.client.delete(f"/api/sessions/{env.jarvis.session_id}").status_code == 409
    assert env.client.post("/api/sessions").status_code == 409
    env.gate.set()
    env.wait_idle()


def test_render_history_pairs_tools_and_hides_summary():
    messages = [
        {"role": "user", "content": "[Summary of the earlier conversation]\n- did things"},
        {"role": "assistant", "content": "Understood — context noted."},
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "call_tool", "arguments": '{"name": "list_dir", "arguments": {"path": "."}}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"artifact_id": "artifact_abc123"}'},
        {"role": "assistant", "content": "Done."},
    ]
    items = render_history(messages, lambda artifact_id: {"id": artifact_id})
    assert [i["type"] for i in items] == ["summary", "user", "tool", "artifact", "assistant"]
    tool = items[2]
    assert tool["name"] == "list_dir" and tool["via"] == "call_tool"
    assert tool["args"] == {"path": "."} and tool["status"] == "done"
    assert items[3]["artifact"] == {"id": "artifact_abc123"}
    assert [i.get("index") for i in items] == [None, 2, 3, None, 5]


# ---------- mind ----------

def test_mind_whitelist(env):
    names = [f["name"] for f in env.client.get("/api/mind").json()["files"]]
    assert names == ["user.md", "system.md", "self.md", "MEMORY.md"]
    assert env.client.put("/api/mind/user.md", json={"content": "Name: Test"}).status_code == 200
    assert env.client.get("/api/mind/user.md").json()["content"] == "Name: Test"
    assert env.client.put("/api/mind/MEMORY.md", json={"content": "x"}).status_code == 403
    assert env.client.get("/api/mind/MEMORY.md").status_code == 200
    assert env.client.get("/api/mind/config.toml").status_code == 404
    assert env.client.put("/api/mind/notes.md", json={"content": "x"}).status_code == 404
    assert env.client.get("/api/mind/..%2Fconfig.toml").status_code == 404
    big = "x" * (64 * 1024 + 1)
    assert env.client.put("/api/mind/user.md", json={"content": big}).status_code == 413


def test_consolidate_runs_in_thread(env, monkeypatch):
    import jarvis.consolidate as consolidate_module
    monkeypatch.setattr(consolidate_module, "consolidate", lambda **kwargs: {"facts_added": 2})
    assert env.client.post("/api/mind/consolidate").json() == {"facts_added": 2}


# ---------- memory / tools / jobs / settings ----------

def test_memory_review_and_forget(env):
    from jarvis.memory import FactStore
    FactStore(env.jarvis.mind).remember("The user likes tea", why="said so", name="tea")
    listing = env.client.get("/api/memory").json()
    assert [m["name"] for m in listing["memories"]] == ["tea"]
    assert env.client.post("/api/memory/tea/review", json={"decision": "reject"}).json()["status"] == "rejected"
    assert env.client.post("/api/memory/tea/review", json={"decision": "bogus"}).status_code == 400
    assert env.client.get("/api/memory/recall?q=tea").status_code == 200
    assert env.client.delete("/api/memory/tea").status_code == 200
    assert env.client.delete("/api/memory/tea").status_code == 404


def test_tools_and_skills(env):
    tools = env.client.get("/api/tools").json()["tools"]
    assert any(t["name"] == "run_command" and t["core"] for t in tools)
    assert "tools" in env.client.get("/api/tools/search?q=read+a+file").json()
    (env.jarvis.mind / "skills" / "demo.md").write_text("# Demo skill\n\n## Steps\n- one\n")
    skills = env.client.get("/api/skills").json()["skills"]
    assert skills[0]["name"] == "demo" and skills[0]["title"] == "Demo skill"


def test_jobs_unknown_name(env):
    assert env.client.get("/api/jobs").status_code == 200
    assert env.client.post("/api/jobs/nothing/enable").status_code == 404
    assert env.client.delete("/api/jobs/bad..name/").status_code in (404, 405)


def test_settings_and_providers(env):
    settings = env.client.get("/api/settings").json()["settings"]
    assert "llm.model" in settings
    assert env.client.post("/api/settings", json={"path": "voice.engine", "value": "kokoro"}).status_code == 200
    assert env.jarvis.config["voice"]["engine"] == "kokoro"
    assert env.client.post("/api/settings", json={"path": "paths.mind", "value": "/tmp"}).status_code == 400
    providers = env.client.get("/api/providers").json()
    assert any(p["id"] == "ollama" for p in providers["providers"])
    assert env.client.post("/api/provider", json={"provider": "nope"}).status_code == 400
    status = env.client.get("/api/status").json()
    assert status["security_mode"] and status["session"]["id"] == env.jarvis.session_id


def test_providers_report_key_status_without_leaking(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env.jarvis.config["llm"]["api_key"] = "sk-session-secret"
    response = env.client.get("/api/providers")
    data = response.json()
    by_id = {p["id"]: p for p in data["providers"]}
    assert by_id["gemini"]["env_set"] is True
    assert by_id["openai"]["env_set"] is False
    assert by_id["custom"]["env_set"] is False
    assert data["session_key"] is True
    assert "super-secret-value" not in response.text
    assert "sk-session-secret" not in response.text


def test_commands_list(env):
    commands = env.client.get("/api/commands").json()["commands"]
    assert any(c["name"] == "provider" and c["subcommands"] for c in commands)


# ---------- artifacts ----------

def test_artifact_file_only_for_images(env, tmp_path):
    png = tmp_path / "pic.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    store = env.state.artifacts
    image = store.create("image", "pic", path=str(png))["artifact_id"]
    fake_image = store.create("image", "sneaky", path=str(secret))["artifact_id"]
    doc = store.create("markdown", "doc", content="# hi", path=str(png))["artifact_id"]
    missing = store.create("image", "gone", path=str(tmp_path / "gone.png"))["artifact_id"]

    assert env.client.get(f"/api/artifacts/{image}/file").content.startswith(b"\x89PNG")
    for refused in (fake_image, doc, missing, "artifact_unknown"):
        assert env.client.get(f"/api/artifacts/{refused}/file").status_code == 404
    assert env.client.get("/api/artifacts/not-an-artifact/file").status_code == 400
    assert env.client.get(f"/api/artifacts/{doc}").json()["title"] == "doc"
    assert len(env.client.get("/api/artifacts").json()["artifacts"]) == 4


# ---------- characters (store faked: module belongs to another workstream) ----------

class FakeStore:
    def __init__(self):
        self.cards = {"jarvis": {"id": "jarvis", "name": "Jarvis", "builtin": True}}

    def list(self):
        return list(self.cards.values())

    def get(self, card_id):
        return self.cards.get(card_id)

    def import_bytes(self, data, filename):
        if not data.startswith(b"{"):
            raise ValueError("not a character card")
        card = {"id": "imported", "name": filename, "builtin": False}
        self.cards["imported"] = card
        return card

    def delete(self, card_id):
        return bool(self.cards.pop(card_id, None)) and card_id != "jarvis"

    def avatar_path(self, card_id):
        return None


def test_character_endpoints(env):
    env.jarvis.store_characters = FakeStore()
    env.jarvis.character = "jarvis"
    env.jarvis.set_character = lambda card_id: (
        {"error": "no such card"} if card_id not in env.jarvis.store_characters.cards
        else {"character": {"id": card_id}})
    assert [c["id"] for c in env.client.get("/api/characters").json()["characters"]] == ["jarvis"]
    data = base64.b64encode(b'{"name": "x"}').decode()
    imported = env.client.post("/api/characters/import", json={"filename": "x.json", "data_base64": data})
    assert imported.json()["id"] == "imported"
    bad = base64.b64encode(b"junk").decode()
    assert env.client.post("/api/characters/import", json={"filename": "x", "data_base64": bad}).status_code == 400
    assert env.client.post("/api/characters/import", json={"filename": "x", "data_base64": "!!"}).status_code == 400
    assert env.client.post("/api/characters/imported/activate").status_code == 200
    assert env.client.post("/api/characters/ghost/activate").status_code == 404
    assert env.client.get("/api/characters/imported/avatar").status_code == 404
    assert env.client.delete("/api/characters/imported").status_code == 200
    assert env.client.delete("/api/characters/jarvis").status_code == 404


def test_character_crud_reset_export(env):
    calls = []

    class Store(FakeStore):
        def save(self, fields, card_id=None):
            if not fields.get("name", "x"):
                raise ValueError("name required")
            card = {"id": card_id or "new", "name": fields.get("name", "n"), **fields}
            calls.append((fields, card_id))
            self.cards[card["id"]] = card
            return {"id": card["id"], "name": card["name"]}

        def reset(self, card_id):
            return card_id == "jarvis"

        def export(self, card_id):
            return {"spec": "chara_card_v2", "data": {"name": self.cards[card_id]["name"]}}

    env.jarvis.store_characters = Store()
    created = env.client.post("/api/characters", json={"fields": {"name": "Ada", "tags": ["a"]}})
    assert created.status_code == 200 and created.json()["card"]["id"] == "new"
    assert env.client.put("/api/characters/new", json={"name": "Ada2"}).json()["character"]["name"] == "Ada2"
    assert calls[-1] == ({"name": "Ada2"}, "new")
    assert env.client.put("/api/characters/ghost", json={"name": "x"}).status_code == 404
    assert env.client.post("/api/characters", json={"name": ""}).status_code == 400  # ValueError
    assert env.client.post("/api/characters", json={"name": 5}).status_code == 400  # wrong type
    assert env.client.post("/api/characters", json={"tags": [1]}).status_code == 400
    assert env.client.post("/api/characters/jarvis/reset").json()["reset"] == "jarvis"
    assert env.client.post("/api/characters/new/reset").status_code == 404
    export = env.client.get("/api/characters/jarvis/export")
    assert export.json()["spec"] == "chara_card_v2"
    assert "jarvis.json" in export.headers["content-disposition"]
    assert env.client.get("/api/characters/ghost/export").status_code == 404
    assert env.client.get("/api/characters/jarvis").json()["name"] == "Jarvis"
    assert env.client.get("/api/characters/..%2Fx").status_code in (400, 404)


def test_chatgpt_auth_endpoints_use_module(env, monkeypatch):
    import sys
    import types

    class FakeAuth:
        def status(self):
            return {"signed_in": False, "pending": False}

        def start_login(self, open_browser=True):
            assert open_browser is False
            return {"auth_url": "https://auth.example/login"}

        def logout(self):
            return {"signed_in": False}

    module = types.ModuleType("jarvis.providers.chatgpt_auth")
    module.get_chatgpt_auth = lambda config=None: FakeAuth()
    monkeypatch.setitem(sys.modules, "jarvis.providers.chatgpt_auth", module)
    assert env.client.get("/api/auth/chatgpt").json()["signed_in"] is False
    assert env.client.post("/api/auth/chatgpt/login").json()["auth_url"].startswith("https://")
    assert env.client.post("/api/auth/chatgpt/logout").status_code == 200


# ---------- SSE hub (unit) ----------

def test_event_message_format():
    event = Event("tool_started", "s1", turn_id="t1", data={"name": "x"})
    message = event_message(event)
    assert message["kind"] == "tool_started" and message["data"] == {"name": "x"}
    assert sse_format(message).startswith("data: {") and sse_format(message).endswith("\n\n")


def test_hub_forwards_from_threads_and_drops_oldest():
    async def scenario():
        bus = EventBus()
        hub = EventHub(bus)
        client = hub.connect()
        worker = threading.Thread(target=lambda: bus.emit(Event("turn_started", "s", data={"n": 1})))
        worker.start()
        first = await asyncio.wait_for(client.queue.get(), 2)
        assert first["kind"] == "turn_started"
        for index in range(MAX_QUEUE + 50):
            client._push({"kind": "x", "n": index})
        assert client.queue.qsize() == MAX_QUEUE
        assert client.queue.get_nowait()["n"] == 50  # oldest 50 dropped
        hub.disconnect(client)
        before = client.queue.qsize()
        bus.emit(Event("late", "s"))
        await asyncio.sleep(0.05)
        assert client.queue.qsize() == before

    asyncio.run(scenario())


# ---------- history editing ----------

def _seed_history(env):
    env.jarvis.history[:] = [
        {"role": "user", "content": "one"}, {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "two"}, {"role": "assistant", "content": "a2"}]


def test_edit_message_runs_in_worker(env, monkeypatch):
    _seed_history(env)
    seen = []
    events = []
    env.jarvis.events.subscribe(lambda event: events.append(event))

    def fake_edit(index, text, **kwargs):
        seen.append((index, text))
        return "ok"

    monkeypatch.setattr(env.jarvis, "edit_message", fake_edit, raising=False)
    assert env.client.post("/api/messages/2/edit", json={"text": " new "}).status_code == 202
    env.wait_idle()
    assert seen == [(2, "new")]
    assert any(e.kind == "busy_changed" and e.data["busy"] is False for e in events)
    assert env.client.post("/api/messages/1/edit", json={"text": "x"}).status_code == 400  # assistant
    assert env.client.post("/api/messages/99/edit", json={"text": "x"}).status_code == 400
    assert env.client.post("/api/messages/-1/edit", json={"text": "x"}).status_code == 400
    assert env.client.post("/api/messages/0/edit", json={"text": "  "}).status_code == 400
    assert len(seen) == 1


def test_edit_and_regenerate_busy_409(env, monkeypatch):
    _seed_history(env)
    env.gate = threading.Event()
    env.client.post("/api/chat", json={"text": "first"})
    assert env.client.post("/api/messages/0/edit", json={"text": "x"}).status_code == 409
    assert env.client.post("/api/regenerate").status_code == 409
    env.gate.set()
    env.wait_idle()


def test_regenerate(env, monkeypatch):
    assert env.client.post("/api/regenerate").status_code == 400  # nothing to regenerate
    _seed_history(env)
    ran = []
    monkeypatch.setattr(env.jarvis, "regenerate", lambda **kw: ran.append(1) or "ok", raising=False)
    assert env.client.post("/api/regenerate").status_code == 202
    env.wait_idle()
    assert ran == [1]


def test_worker_error_becomes_event(env, monkeypatch):
    _seed_history(env)
    events = []
    env.jarvis.events.subscribe(lambda event: events.append(event))

    def boom(**kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(env.jarvis, "regenerate", boom, raising=False)
    env.client.post("/api/regenerate")
    env.wait_idle()
    assert any(e.kind == "error" and "kaboom" in e.data["message"] for e in events)


# ---------- voice ----------

class FakeEngine:
    def __init__(self, ready=True, missing=None):
        self._ready, self._missing, self.texts = ready, missing, []

    def ready(self):
        return self._ready, self._missing

    def synthesize(self, text):
        self.texts.append(text)
        return b"RIFFfakewav"


CATALOG = [{"id": "piper:en_US-lessac-medium", "kind": "tts", "engine": "piper", "label": "Lessac",
            "size_mb": 60, "installed": False, "package": None, "package_installed": True, "default": True}]


@pytest.fixture
def voice_env(env, monkeypatch, tmp_path):
    import jarvis.models_manager as models_manager
    import jarvis.voice as voice

    env.engines = []
    env.engine_ready = (True, None)

    def make_engine(config):
        engine = FakeEngine(*env.engine_ready)
        env.engines.append((dict(config["voice"]), engine))
        return engine

    monkeypatch.setattr(voice, "make_engine", make_engine, raising=False)
    monkeypatch.setattr(models_manager, "catalog", lambda config: CATALOG, raising=False)
    env.models_manager = models_manager
    return env


def test_voice_info_and_snapshot(voice_env):
    info = voice_env.client.get("/api/voice").json()
    assert info["auto_speak"] is False and info["engine"] == "piper"
    assert info["ready"] is True and info["missing"] is None
    assert info["models"] == CATALOG and info["downloads"] == {}
    assert voice_env.client.get("/api/snapshot").json()["voice"]["engine"] == "piper"
    assert voice_env.client.get("/api/models/catalog").json() == CATALOG


def test_voice_settings_persist_and_refresh(voice_env, monkeypatch):
    refreshed = []
    monkeypatch.setattr(voice_env.jarvis, "refresh_voice", lambda: refreshed.append(1))
    response = voice_env.client.post("/api/voice/settings", json={
        "auto_speak": True, "engine": "kokoro", "kokoro_voice": "af_bella", "piper_voice": "en_GB-alan-medium"})
    assert response.status_code == 200
    voice = voice_env.jarvis.config["voice"]
    assert (voice["enabled"], voice["engine"], voice["kokoro_voice"], voice["model"]) == (
        True, "kokoro", "af_bella", "en_GB-alan-medium")
    assert refreshed == [1]
    from jarvis.config import runtime_config_path
    import json
    saved = json.loads(runtime_config_path(voice_env.jarvis.config).read_text())["voice"]
    assert saved["enabled"] is True and saved["engine"] == "kokoro" and saved["model"] == "en_GB-alan-medium"
    assert response.json()["engine"] == "kokoro"
    assert voice_env.client.post("/api/voice/settings", json={"engine": "bogus"}).status_code == 400
    assert voice_env.client.post("/api/voice/settings", json={"piper_voice": "../x"}).status_code == 400


def test_tts_returns_wav_and_caches_engine(voice_env):
    first = voice_env.client.post("/api/voice/tts", json={"text": "hello"})
    assert first.status_code == 200 and first.headers["content-type"] == "audio/wav"
    assert first.content == b"RIFFfakewav"
    voice_env.client.post("/api/voice/tts", json={"text": "again"})
    built = len(voice_env.engines)
    assert built == 1  # cached across calls (and across GET /api/voice)
    voice_env.client.post("/api/voice/settings", json={"engine": "kokoro"})
    voice_env.client.post("/api/voice/tts", json={"text": "x"})
    assert len(voice_env.engines) == built + 1 and voice_env.engines[-1][0]["engine"] == "kokoro"


def test_tts_validation_and_not_ready(voice_env):
    assert voice_env.client.post("/api/voice/tts", json={"text": "  "}).status_code == 400
    assert voice_env.client.post("/api/voice/tts", json={"text": "x" * 5001}).status_code == 413
    voice_env.engine_ready = (False, "piper:en_US-lessac-medium")
    voice_env.state.invalidate_tts()
    response = voice_env.client.post("/api/voice/tts", json={"text": "hi"})
    assert response.status_code == 424
    assert response.json()["needs_download"] == "piper:en_US-lessac-medium"
    assert response.json()["size_mb"] == 60 and response.json()["label"] == "Lessac"


def test_voice_download_progress_and_duplicates(voice_env, monkeypatch):
    gate = threading.Event()
    events = []
    voice_env.jarvis.events.subscribe(lambda event: events.append(event))

    def fake_download(item_id, progress=None, config=None):
        progress({"item_id": item_id, "status": "downloading", "progress": 0.5, "message": ""})
        gate.wait(5)
        progress({"item_id": item_id, "status": "done", "progress": 1.0, "message": ""})

    monkeypatch.setattr(voice_env.models_manager, "download", fake_download, raising=False)
    item = "piper:en_US-lessac-medium"
    assert voice_env.client.post("/api/voice/download", json={"item_id": "nope"}).status_code == 400
    assert voice_env.client.post("/api/voice/download", json={"item_id": item}).status_code == 202
    end = time.time() + 3
    while item not in voice_env.state.downloads and time.time() < end:
        time.sleep(0.02)
    assert voice_env.client.post("/api/voice/download", json={"item_id": item}).status_code == 409
    assert voice_env.client.get("/api/voice").json()["downloads"][item]["progress"] == 0.5
    gate.set()
    end = time.time() + 3
    while voice_env.state.active_downloads and time.time() < end:
        time.sleep(0.02)
    kinds = [e.data["status"] for e in events if e.kind == "voice_download"]
    assert kinds == ["downloading", "done"]
    assert voice_env.client.post("/api/voice/download", json={"item_id": item}).status_code == 202
    gate.set()


def test_voice_download_failure_reports_error(voice_env, monkeypatch):
    events = []
    voice_env.jarvis.events.subscribe(lambda event: events.append(event))

    def fail_download(item_id, progress=None, config=None):
        raise OSError("disk full")

    monkeypatch.setattr(voice_env.models_manager, "download", fail_download, raising=False)
    voice_env.client.post("/api/voice/download", json={"item_id": "piper:en_US-lessac-medium"})
    end = time.time() + 3
    while voice_env.state.active_downloads and time.time() < end:
        time.sleep(0.02)
    errors = [e.data for e in events if e.kind == "voice_download" and e.data["status"] == "error"]
    assert errors and "disk full" in errors[0]["message"]


def test_no_server_side_auto_speak(voice_env, monkeypatch):
    spoken = []
    voice_env.jarvis.config["voice"]["enabled"] = True
    monkeypatch.setattr(voice_env.jarvis.voice, "speak", lambda text: spoken.append(text))
    voice_env.client.post("/api/chat", json={"text": "hi"})
    voice_env.wait_idle()
    assert spoken == []


# ---------- onboarding & providers ----------

def test_onboarding_flag_persists(env):
    first = env.client.get("/api/onboarding").json()
    assert first["done"] is False and first["provider"] and "model" in first
    assert env.client.post("/api/onboarding/done").json() == {"done": True}
    assert env.client.get("/api/onboarding").json()["done"] is True
    assert env.client.get("/api/snapshot").json()["onboarded"] is True
    assert (env.jarvis.mind / "ui_state.json").exists()
    assert not list(env.jarvis.mind.glob("*.tmp"))


def test_providers_have_labels_kinds_and_order(env):
    data = env.client.get("/api/providers").json()
    providers = data["providers"]
    assert providers[0]["id"] == "chatgpt" and providers[0]["kind"] == "subscription"
    by_id = {p["id"]: p for p in providers}
    assert "Plus/Pro" in by_id["chatgpt"]["label"] and "API" in by_id["openai"]["label"]
    assert by_id["openai"]["kind"] == "api_key" and by_id["ollama"]["kind"] == "local"
    kinds = [p["kind"] for p in providers]
    assert kinds.index("local") < kinds.index("api_key")
    assert all(p["label"] and p["kind"] for p in providers)
    assert [p["id"] for p in providers if p["active"]] == [data["active"]]


def test_edit_and_regenerate_with_real_agent(env):
    if not hasattr(env.jarvis, "edit_message"):
        pytest.skip("agent.edit_message not available")
    env.client.post("/api/chat", json={"text": "first"})
    env.wait_idle()
    env.client.post("/api/chat", json={"text": "second"})
    env.wait_idle()
    assert env.client.post("/api/messages/2/edit", json={"text": "edited"}).status_code == 202
    env.wait_idle()
    texts = [m["content"] for m in env.jarvis.history if m["role"] == "user"]
    assert texts == ["first", "edited"]
    assert env.client.post("/api/regenerate").status_code == 202
    env.wait_idle()
    assert [m["role"] for m in env.jarvis.history] == ["user", "assistant", "user", "assistant"]
    assert [i["index"] for i in env.client.get("/api/snapshot").json()["history"]] == [0, 1, 2, 3]


def test_preset_with_typed_credential_keeps_preset_backend(env, monkeypatch):
    """A typed key/env var for e.g. Anthropic must not turn it into 'custom'."""
    import jarvis.commands as commands
    monkeypatch.setattr(commands, "_test_connection",
                        lambda *a, **k: (False, "offline in tests", [], "x"))
    monkeypatch.setenv("MY_CLAUDE_KEY", "sk-test")
    data = env.client.post("/api/provider", json={"provider": "anthropic",
                                                   "api_key_env": "MY_CLAUDE_KEY"}).json()
    llm = env.jarvis.config["llm"]
    assert llm["provider"] == "anthropic" and env.jarvis.llm.backend == "anthropic"
    assert llm["api_key_env"] == "MY_CLAUDE_KEY" and not llm.get("api_key")
    assert "env var MY_CLAUDE_KEY" in data["output"]
    # A literal key is used for this run only and never persisted.
    env.client.post("/api/provider", json={"provider": "groq", "api_key_env": "gsk-literal-value"})
    assert env.jarvis.config["llm"]["api_key"] == "gsk-literal-value"
    from jarvis.config import runtime_config_path
    assert "gsk-literal-value" not in runtime_config_path(env.jarvis.config).read_text()


def test_models_endpoint_reports_unreachable_server_without_500(env, monkeypatch):
    import jarvis.commands as commands

    def boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(commands, "_test_connection", boom)
    r = env.client.get("/api/models")
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["models"] == []


def test_providers_expose_default_model(env):
    by_id = {p["id"]: p for p in env.client.get("/api/providers").json()["providers"]}
    for name in ("openai", "gemini", "openrouter", "groq", "deepseek", "mistral", "xai", "anthropic"):
        assert by_id[name]["default_model"], name
    assert by_id["gemini"]["default_model"].startswith("gemini")
    assert all(not by_id[n]["default_model"] for n in ("lmstudio", "ollama", "llama.cpp", "vllm", "custom"))


def test_switching_provider_resets_foreign_model(env, monkeypatch):
    import jarvis.commands as commands
    monkeypatch.setattr(commands, "_test_connection", lambda *a, **k: (True, "ok", [], "x"))
    env.jarvis.config["llm"]["model"] = "some-deepseek-id"
    env.jarvis.config["llm"]["provider"] = "deepseek"
    env.client.post("/api/provider", json={"provider": "openai"})
    assert env.jarvis.config["llm"]["model"] == "gpt-5"  # preset default, not the old id
    # An explicit model still wins.
    env.client.post("/api/provider", json={"provider": "gemini", "model": "gemini-2.5-pro"})
    assert env.jarvis.config["llm"]["model"] == "gemini-2.5-pro"
    # Hosted -> local server with nothing remembered: no stale hosted id.
    env.client.post("/api/provider", json={"provider": "vllm"})
    assert env.jarvis.config["llm"]["model"] == ""


def test_provider_test_uses_typed_literal_key_and_never_echoes_it(env, monkeypatch):
    import jarvis.commands as commands
    seen = {}

    def fake(jarvis, **kwargs):
        seen.update(kwargs)
        return False, "401 bad key sk-secret-123", [], kwargs.get("base_url") or "x"
    monkeypatch.setattr(commands, "_test_connection", fake)
    out = env.client.post("/api/provider/test", json={"provider": "gemini", "api_key_env": "sk-secret-123"}).json()
    assert seen["api_key"] == "sk-secret-123" and not seen["api_key_env"]
    assert seen["base_url"].startswith("https://generativelanguage")
    assert "sk-secret-123" not in str(out)
    # Applying with a literal key keeps it out of the response and off disk.
    out = env.client.post("/api/provider", json={"provider": "groq", "api_key_env": "gsk-secret-456"}).json()
    assert "gsk-secret-456" not in out["output"]


def test_provider_fields_are_listed_saved_and_validated(env, monkeypatch):
    import jarvis.commands as commands
    from jarvis import provider_fields
    from jarvis.providers import PRESETS
    seen = {}

    def fake(jarvis, **kwargs):
        seen.update(kwargs)
        return True, "ok", [], kwargs.get("base_url") or jarvis.config["llm"]["base_url"]
    monkeypatch.setattr(commands, "_test_connection", fake)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    account = PRESETS["cloudflare"]["fields"][0]
    # Testing tries typed values without saving them.
    env.client.post("/api/provider/test", json={"provider": "cloudflare", "fields": {"account_id": "typed1"}})
    assert seen["fields"] == {"account_id": "typed1"} and provider_fields.saved(account) == ""
    # A bad value changes nothing.
    before = env.jarvis.config["llm"]["provider"]
    bad = env.client.post("/api/provider", json={"provider": "cloudflare", "fields": {"account_id": "a/b"}})
    assert bad.status_code == 400 and env.jarvis.config["llm"]["provider"] == before
    out = env.client.post("/api/provider", json={
        "provider": "cloudflare", "fields": {"account_id": "acc123"}})
    assert out.status_code == 200
    assert env.jarvis.llm.backend == "cloudflare"
    assert "/accounts/acc123/ai/v1" in str(env.jarvis.llm.client.base_url)
    by_id = {p["id"]: p for p in env.client.get("/api/providers").json()["providers"]}
    shown = by_id["cloudflare-gateway"]["fields"]
    assert [(f["key"], f["value"], f["set"]) for f in shown] == [
        ("account_id", "acc123", True), ("gateway_id", "", True)]
    # The slash command takes the same settings.
    commands.handle_slash_command(env.jarvis, "/provider use cloudflare-gateway gateway_id=my-gw")
    assert provider_fields.values("cloudflare-gateway") == {"account_id": "acc123", "gateway_id": "my-gw"}


def test_llm_options_route_applies_and_persists_only_those_keys(env):
    response = env.client.post("/api/llm/options", json={"effort": "High", "max_tokens": 32768})
    assert response.status_code == 200
    assert response.json()["effort"] == "high" and response.json()["status"]["max_tokens"] == 32768
    assert env.jarvis.llm.max_tokens == 32768 and env.jarvis.config["llm"]["effort"] == "high"
    from jarvis.config import runtime_config_path
    import json
    saved = json.loads(runtime_config_path(env.jarvis.config).read_text())
    assert saved == {"llm": {"effort": "high", "max_tokens": 32768}}
    assert env.client.post("/api/llm/options", json={"effort": ""}).json()["effort"] == ""
    assert env.client.post("/api/llm/options", json={"effort": "extreme"}).status_code == 400
    assert env.client.post("/api/llm/options", json={"max_tokens": 5}).status_code == 400
    assert env.client.get("/api/settings").json()["settings"]["llm.effort"] == ""
