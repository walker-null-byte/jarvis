"""Agent profiles, Runtimes, context-local tool context, session kinds, runtime routes."""

import json
import threading
import time

import pytest

pytest.importorskip("fastapi")

from jarvis import runtime, toolctx
from jarvis.agent import Jarvis
from jarvis.llm import LLM
from jarvis.runtime import AgentProfile, Runtimes, llm_overrides, register_profile_factory
from jarvis.sessions import SessionStore
from tests.test_webui import BASE, TOKEN, Env, _config, env  # noqa: F401


def _tool_call(name, args, call_id="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def fake_llm(monkeypatch):
    """LLM.chat that records requests and replays `script` (a list of replies; text after that)."""
    state = type("S", (), {"script": [], "calls": [], "gate": None})()

    def chat(llm_self, messages, tools=None, on_text=None, **kw):
        state.calls.append({"llm": llm_self, "messages": messages, "tools": tools})
        if state.gate is not None:
            state.gate.wait(5)
        if state.script:
            return state.script.pop(0)
        if on_text:
            on_text("done")
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(LLM, "chat", chat)
    return state


def _names(tools):
    return {t["function"]["name"] for t in tools}


# ---------- llm_overrides ----------

def test_llm_overrides_presets(tmp_path):
    config = _config(tmp_path)
    gem = llm_overrides("gemini", config=config)
    assert gem["provider"] == "gemini" and gem["api_key_env"] == "GEMINI_API_KEY"
    assert gem["model"] == "gemini-2.5-flash" and gem["base_url"].startswith("https://generativelanguage")
    assert llm_overrides("gemini", "gemini-2.5-pro", config=config)["model"] == "gemini-2.5-pro"
    claude = llm_overrides("anthropic", config=config)
    assert claude["api_key_env"] == "ANTHROPIC_API_KEY" and claude["provider"] == "anthropic"
    assert llm_overrides("chatgpt", config=config)["api_key_env"] is None
    assert "model" not in llm_overrides("lmstudio", config=config)  # local: nothing loaded known
    assert llm_overrides("weird", "m") == {"provider": "weird", "model": "m"}


def test_llm_overrides_prefers_remembered_model(tmp_path):
    from jarvis.config import remember_model
    config = _config(tmp_path)
    (tmp_path / "mind").mkdir()
    remember_model(config, "lmstudio", "gemma4")
    assert llm_overrides("lmstudio", config=config)["model"] == "gemma4"


# ---------- profile behaviour ----------

def test_no_profile_is_unchanged(tmp_path, fake_llm):
    config = _config(tmp_path)
    jarvis = Jarvis(config)
    assert jarvis.config is config and jarvis.profile is None
    assert jarvis.sessions.get(jarvis.session_id)["kind"] == "chat"


def test_profile_gets_own_config_and_llm(tmp_path, fake_llm):
    config = _config(tmp_path)
    profile = AgentProfile(kind="agent", owner="lead", title="Lead run",
                           llm=llm_overrides("gemini", "g-test", config=config), max_tool_rounds=3)
    jarvis = Jarvis(config, profile=profile)
    assert jarvis.config is not config
    assert jarvis.config["llm"]["provider"] == "gemini" and jarvis.config["llm"]["model"] == "g-test"
    assert config["llm"]["provider"] != "gemini"
    assert jarvis.llm.model == "g-test"
    meta = jarvis.sessions.get(jarvis.session_id)
    assert (meta["kind"], meta["owner"], meta["title"]) == ("agent", "lead", "Lead run")


def test_system_prompt_appended_or_replacing_persona(tmp_path, fake_llm):
    config = _config(tmp_path)
    base = Jarvis(config)._build_prompt()
    appended = Jarvis(config, profile=AgentProfile(system_prompt="ROLE: lead finder"))._build_prompt()
    replaced = Jarvis(config, profile=AgentProfile(system_prompt="ROLE: lead finder",
                                                   replace_persona=True))._build_prompt()
    assert "ROLE: lead finder" not in base
    assert "ROLE: lead finder" in appended and len(appended) > len(base)
    assert "ROLE: lead finder" in replaced and len(replaced) < len(appended)


def test_extra_hidden_and_core_tools(tmp_path, fake_llm):
    config = _config(tmp_path)

    def shout(text: str):
        """Shout the text back."""
        return text.upper()

    profile = AgentProfile(extra_tools={"shout": shout}, hidden_tools={"write_file", "cron_add"},
                           core_tools=["get_current_time"])
    jarvis = Jarvis(config, profile=profile)
    tools = jarvis.exposure.tools()
    names = _names(tools)
    assert "shout" in names and "get_current_time" in names and "write_file" not in names
    schema = next(t for t in tools if t["function"]["name"] == "shout")["function"]
    assert schema["description"] == "Shout the text back."
    assert schema["parameters"]["properties"]["text"]["type"] == "string"
    # extra tools run (and validate); hidden ones are invisible and refused
    assert jarvis.exposure.run("shout", {"text": "hi"}) == "HI"
    assert jarvis.exposure.run("shout", {})["validation_error"]
    assert "Unknown tool" in jarvis.exposure.run("write_file", {"file_path": "x", "content": "y"})["error"]
    found = jarvis.exposure.find_tools("write a file cron schedule")
    listed = {t["name"] for t in found.get("tools", [])} | {t["name"] for t in found.get("available", [])}
    assert not listed & {"write_file", "cron_add"}
    prompt = jarvis.exposure.prompt_section()
    assert "write_file" not in prompt and "cron_add" not in prompt and "shout" in prompt
    # the unprofiled agent still sees them
    plain = Jarvis(config)
    assert "write_file" in plain.registry.tool_names() and "write_file" in plain.exposure.prompt_section()


def test_hidden_tool_in_all_mode(tmp_path, fake_llm):
    config = _config(tmp_path)
    config["agent"]["tool_mode"] = "all"
    jarvis = Jarvis(config, profile=AgentProfile(hidden_tools={"run_command"}))
    assert "run_command" not in _names(jarvis.exposure.tools())
    assert "list_dir" in _names(jarvis.exposure.tools())


def test_max_tool_rounds_override(tmp_path, fake_llm):
    config = _config(tmp_path)
    fake_llm.script = [_tool_call("get_current_time", {}, f"c{i}") for i in range(10)]
    jarvis = Jarvis(config, profile=AgentProfile(max_tool_rounds=2))
    jarvis.ask("loop")
    rounds = [c for c in fake_llm.calls if c["tools"]]
    assert len(rounds) == 3  # 2 tool rounds + the wrap-up call


def test_persist_false_writes_nothing(tmp_path, fake_llm):
    config = _config(tmp_path)
    jarvis = Jarvis(config, profile=AgentProfile(kind="subagent", persist=False))
    jarvis.ask("hello")
    assert [m["role"] for m in jarvis.history] == ["user", "assistant"]
    sessions = tmp_path / "mind" / "sessions"
    assert not list(sessions.glob("*")) if sessions.exists() else True
    assert jarvis.sessions.get(jarvis.session_id)["kind"] == "subagent"


# ---------- session kinds ----------

def test_session_kind_and_owner_filters(tmp_path):
    store = SessionStore(tmp_path)
    ids = {
        "chat": store.create("normal chat"),
        "a1": store.create("run 1", kind="agent", owner="lead-finder"),
        "a2": store.create("run 2", kind="agent", owner="other"),
        "d": store.create("talk", kind="discussion", owner="disc1"),
    }
    for sid in ids.values():
        store.append(sid, {"role": "user", "content": "findme"})
    # a legacy session: no kind key at all
    legacy = tmp_path / "sessions" / "legacy.json"
    legacy.write_text(json.dumps({"id": "legacy", "title": "old", "updated": "2020-01-01"}))
    (tmp_path / "sessions" / "legacy.jsonl").write_text("")
    chats = {s["id"] for s in store.list()}
    assert chats == {ids["chat"], "legacy"}
    assert {s["id"] for s in store.list(kind="agent")} == {ids["a1"], ids["a2"]}
    assert {s["id"] for s in store.list(kind="agent", owner="lead-finder")} == {ids["a1"]}
    assert len(store.list(kind=None)) == 5 and len(store.list(kind="all")) == 5
    assert {s["id"] for s in store.search("findme", kind="discussion")} == {ids["d"]}
    assert {s["id"] for s in store.search("findme")} == {ids["chat"]}


def test_sessions_endpoint_kind_param(env):
    store = env.jarvis.sessions
    agent = store.create("agent run", kind="agent", owner="lead")
    store.append(agent, {"role": "user", "content": "x"})
    chat_ids = {s["id"] for s in env.client.get("/api/sessions").json()["sessions"]}
    assert agent not in chat_ids
    got = env.client.get("/api/sessions", params={"kind": "agent", "owner": "lead"}).json()["sessions"]
    assert [s["id"] for s in got] == [agent]
    assert any(s["id"] == agent for s in env.client.get("/api/sessions", params={"kind": "all"}).json()["sessions"])
    # any session can still be resumed by id
    assert env.client.post(f"/api/sessions/{agent}/resume").status_code == 200


# ---------- context-local tool context ----------

def test_each_instance_reaches_its_own_approval_manager(tmp_path, fake_llm):
    config = _config(tmp_path)
    config["security"]["mode"] = "guarded"
    seen = {"a": [], "b": []}
    a, b = Jarvis(config), Jarvis(config)  # b is last: it would win the old global
    a.approvals.set_fallback(lambda action, payload: seen["a"].append(action) or False)
    b.approvals.set_fallback(lambda action, payload: seen["b"].append(action) or False)
    for jarvis in (a, b):
        fake_llm.script = [_tool_call("run_command", {"command": "echo hi > /dev/null"})]
        jarvis.ask("go")
    assert seen == {"a": ["run_command"], "b": ["run_command"]}


def test_tool_context_set_during_tool_and_reset_after(tmp_path, fake_llm):
    config = _config(tmp_path)
    captured = []

    def probe():
        """Record the tool context."""
        captured.append(toolctx.current())
        return "ok"

    jarvis = Jarvis(config, profile=AgentProfile(extra_tools={"probe": probe}))
    fake_llm.script = [_tool_call("probe", {})]
    jarvis.ask("go")
    ctx = captured[0]
    assert ctx.agent is jarvis and ctx.approvals is jarvis.approvals
    assert ctx.cancel_event is jarvis._cancel_event and ctx.session_id == jarvis.session_id
    assert toolctx.current() is None


def test_thread_helpers_keep_context_in_threads():
    ctx = toolctx.ToolContext(agent="me")
    with toolctx.tool_context(ctx):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(1) as pool:
            plain = pool.submit(toolctx.current).result()
            kept = toolctx.submit_in_context(pool, toolctx.current).result()
            wrapped = pool.submit(toolctx.capture(toolctx.current)).result()
    assert plain is None and kept is ctx and wrapped is ctx


def test_approval_request_event_carries_session_of_running_agent(tmp_path, fake_llm):
    config = _config(tmp_path)
    config["security"]["approval_timeout"] = 1
    jarvis = Jarvis(config)
    events = []
    jarvis.events.subscribe(events.append)
    fake_llm.script = [_tool_call("run_command", {"command": "echo hi > /dev/null"})]
    jarvis.ask("go")
    requested = [e for e in events if e.kind == "approval_requested"]
    assert requested and requested[0].session_id == jarvis.session_id and requested[0].turn_id


# ---------- Runtimes + routes ----------

def _wait(cond, timeout=5):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.02)
    assert cond()


def test_runtimes_create_turn_busy_cancel(env, fake_llm):
    rt = env.state.runtimes
    jarvis = rt.create(AgentProfile(kind="agent", owner="lead", title="t"))
    sid = jarvis.session_id
    assert jarvis.events is env.jarvis.events and rt.get(sid) is jarvis
    fake_llm.gate = threading.Event()
    assert rt.start_turn(sid, "hello") is True
    assert rt.busy(sid) and rt.start_turn(sid, "again") is False
    assert not env.state.busy  # the main chat is independent
    fake_llm.gate.set()
    _wait(lambda: not rt.busy(sid))
    assert [m["role"] for m in jarvis.history] == ["user", "assistant"]
    with pytest.raises(KeyError):
        rt.start_turn("nope", "x")
    assert rt.get("nope") is None


def test_runtime_routes(env, fake_llm):
    client = env.client
    sid = env.state.runtimes.create(AgentProfile(kind="agent", owner="lead")).session_id
    assert client.post("/api/runtime/missing/chat", json={"text": "x"}).status_code == 404
    assert client.post(f"/api/runtime/{sid}/chat", json={"text": "  "}).status_code == 400
    assert client.post(f"/api/runtime/{sid}/retry").status_code == 400  # nothing yet
    fake_llm.gate = threading.Event()
    assert client.post(f"/api/runtime/{sid}/chat", json={"text": "hi"}).status_code == 202
    assert client.post(f"/api/runtime/{sid}/chat", json={"text": "hi"}).status_code == 409
    assert client.post(f"/api/runtime/{sid}/cancel").json() == {"cancelled": True}
    fake_llm.gate.set()
    _wait(lambda: not env.state.runtimes.busy(sid))
    history = client.get(f"/api/runtime/{sid}/history").json()
    assert history["history"][0] == {"type": "user", "content": "hi", "index": 0}
    assert history["session"]["kind"] == "agent" and history["busy"] is False
    assert client.post(f"/api/runtime/{sid}/retry").status_code == 202
    _wait(lambda: not env.state.runtimes.busy(sid))
    assert client.get(f"/api/runtime/{sid}/history").json()["history"][-1]["type"] == "assistant"


def test_approvals_from_runtime_sessions_tracked_and_resolved(env, fake_llm):
    jarvis = env.state.runtimes.create(AgentProfile(kind="agent", owner="lead"))
    fake_llm.script = [_tool_call("run_command", {"command": "echo hi > /dev/null"})]
    assert env.state.runtimes.start_turn(jarvis.session_id, "go")
    _wait(lambda: env.state.pending_approvals)
    request_id, pending = next(iter(env.state.pending_approvals.items()))
    assert pending["action"] == "run_command"
    assert env.state.runtimes.find_approval(request_id) is jarvis
    assert env.client.post(f"/api/approvals/{request_id}", json={"approved": True}).status_code == 200
    _wait(lambda: not env.state.runtimes.busy(jarvis.session_id))
    assert not env.state.pending_approvals
    assert env.client.post("/api/approvals/nope", json={"approved": True}).status_code == 404


def test_profile_factory_rebuilds_profile_on_reload(env, fake_llm, monkeypatch):
    monkeypatch.setattr(runtime, "_factories", {})
    register_profile_factory("agent", lambda owner, meta: AgentProfile(
        kind="agent", owner=owner, system_prompt=f"ROLE {owner}"))
    first = env.state.runtimes.create(AgentProfile(kind="agent", owner="lead"))
    first.ask("hi")
    fresh = Runtimes(env.jarvis.config, env.jarvis.events)
    again = fresh.get(first.session_id)
    assert again is not first and again.profile.owner == "lead"
    assert "ROLE lead" in again._build_prompt()
    assert len(again.history) == 2


def test_state_placeholders_are_lazy_and_import_on_use(env):
    assert env.state._agents is None and env.state._runtimes is None
    for name in ("agents", "discussions", "routines"):
        assert hasattr(type(env.state), name)
