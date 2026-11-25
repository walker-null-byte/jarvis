"""MCP client against a tiny local stdio server (tests/mcp_echo_server.py): no network."""

import copy
import json
import sys
import time
from pathlib import Path

import pytest

from jarvis import keystore, mcp_client, toolctx
from jarvis.config import DEFAULTS
from jarvis.registry import ToolRegistry
from jarvis.tool_exposure import ToolExposure

SERVER = str(Path(__file__).parent / "mcp_echo_server.py")


class Approver:
    """Stands in for ApprovalManager: records requests, answers with `answer`."""

    def __init__(self, answer=True):
        self.answer, self.requests, self.timeout = answer, [], 1

    def request(self, action, payload, session_id="", turn_id=""):
        self.requests.append((action, payload))
        return self.answer


@pytest.fixture
def env(tmp_path):
    config = copy.deepcopy(DEFAULTS)
    config["paths"]["mind"] = str(tmp_path / "mind")
    config["agent"]["semantic_search"] = False
    (tmp_path / "mind").mkdir()
    registry = ToolRegistry(config)
    manager = mcp_client.McpManager(registry, tmp_path / "mind")
    approver = Approver()
    registry.set_approval_handler(approver)
    yield type("E", (), dict(config=config, registry=registry, manager=manager, approver=approver, mind=tmp_path / "mind"))
    manager.shutdown()


def echo_spec(**extra):
    return {"command": sys.executable, "args": [SERVER], **extra}


def wait_for(manager, name, status, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        entry = next((s for s in manager.list() if s["name"] == name), None)
        if entry and entry["status"] == status:
            return entry
        time.sleep(0.1)
    raise AssertionError(f"{name} never became {status}: {manager.list()}")


def run_as(env, name, args, session="s1"):
    ctx = toolctx.ToolContext(env.approver, None, None, session, "t")
    with toolctx.tool_context(ctx):
        return env.registry.call(name, args)


def test_parse_servers_accepts_all_shapes():
    one = {"command": "npx", "args": ["-y", "pkg"]}
    assert mcp_client.parse_servers({"mcpServers": {"a": one}}) == {"a": one}
    assert mcp_client.parse_servers({"a": one}) == {"a": one}
    assert mcp_client.parse_servers({"name": "a", **one}) == {"a": one}
    assert mcp_client.parse_servers(json.dumps({"name": "r", "url": "https://x/mcp"})) == {"r": {"url": "https://x/mcp"}}
    for bad in ("not json", "[]", {"a": {"args": []}}, {"bad name": one}, {"a": {"command": "x", "args": "y"}}):
        with pytest.raises(ValueError):
            mcp_client.parse_servers(bad)


def test_env_expansion_uses_keystore_then_environment(monkeypatch):
    keystore.set("FROM_STORE", "stored")
    monkeypatch.setenv("FROM_STORE", "env-loses")
    monkeypatch.setenv("FROM_ENV", "env")
    assert mcp_client.expand("a ${FROM_STORE} b ${FROM_ENV} c ${MISSING_ONE}") == "a stored b env c "


def test_stdio_server_lists_and_calls_tools(env):
    keystore.set("ECHO_SECRET", "hunter2")
    env.manager.add({"name": "echo", **echo_spec(env={"MY_VALUE": "${ECHO_SECRET}"})})
    entry = wait_for(env.manager, "echo", "connected")
    assert entry["tool_count"] == 5 and "echo" in entry["tools"]
    names = env.registry.tool_names()
    assert "mcp__echo__echo" in names and "mcp__echo__add" in names
    assert "echo" in env.registry.get_registry().tools["mcp__echo__echo"]["description"]
    assert env.registry.get_registry().tools["mcp__echo__echo"]["input_schema"]["required"] == ["text"]
    assert run_as(env, "mcp__echo__echo", {"text": "hi"}) == "echo: hi"
    assert run_as(env, "mcp__echo__env_value", {"name": "MY_VALUE"}) == "hunter2"  # ${VAR} filled from keystore
    assert env.approver.requests == []  # readOnlyHint tools never ask
    assert "boom" in json.dumps(run_as(env, "mcp__echo__boom", {}))  # server errors come back as errors
    assert "error" in env.registry.call("mcp__echo__echo", {})  # schema validation happens first
    saved = json.loads((env.mind / "mcp.json").read_text())
    assert saved["mcpServers"]["echo"]["env"]["MY_VALUE"] == "${ECHO_SECRET}"  # the file keeps the reference


def test_mcp_tools_stay_hidden_but_findable(env):
    env.manager.add({"name": "echo", **echo_spec()})
    wait_for(env.manager, "echo", "connected")
    exposure = ToolExposure(env.registry, env.config, env.mind)
    fixed = [t["function"]["name"] for t in exposure.tools()]
    assert not any(n.startswith("mcp__") for n in fixed)
    assert "mcp__echo__add" in exposure.hidden_names()
    found = [t["name"] for t in exposure.find_tools("add two integers", 5)["tools"]]
    assert "mcp__echo__add" in found
    assert "MCP tools from echo" in exposure.prompt_section()
    assert exposure.is_read_only("mcp__echo__echo") and not exposure.is_read_only("mcp__echo__add")
    assert not exposure.is_read_only("mcp__echo__wipe")  # destructive wins over any hint
    env.config["agent"]["core_tools"] = [*env.config["agent"]["core_tools"], "mcp__echo__add"]
    exposure = ToolExposure(env.registry, env.config, env.mind)
    assert "mcp__echo__add" in [t["function"]["name"] for t in exposure.tools()]  # only when listed in core_tools


def test_approvals_per_server_per_session_trust_and_destructive(env):
    env.manager.add({"name": "echo", **echo_spec()})
    wait_for(env.manager, "echo", "connected")
    assert run_as(env, "mcp__echo__add", {"a": 1, "b": 2}) == "3"
    assert len(env.approver.requests) == 1 and env.approver.requests[0][0] == "mcp_tool"
    assert run_as(env, "mcp__echo__add", {"a": 2, "b": 2}) == "4"
    assert len(env.approver.requests) == 1  # approved for the rest of this session
    run_as(env, "mcp__echo__add", {"a": 1, "b": 1}, session="other")
    assert len(env.approver.requests) == 2  # a different session asks again
    run_as(env, "mcp__echo__wipe", {"name": "x"})
    run_as(env, "mcp__echo__wipe", {"name": "y"})
    assert len(env.approver.requests) == 4  # destructive: every time
    env.approver.answer = False
    assert run_as(env, "mcp__echo__add", {"a": 1, "b": 1}, session="third")["approval_required"] is True
    env.approver.answer = True
    env.manager.update("echo", trust=True)
    wait_for(env.manager, "echo", "connected")
    before = len(env.approver.requests)
    assert run_as(env, "mcp__echo__add", {"a": 5, "b": 5}, session="fourth") == "10"
    assert len(env.approver.requests) == before  # trusted server: no prompt
    run_as(env, "mcp__echo__wipe", {"name": "z"}, session="fourth")
    assert len(env.approver.requests) == before + 1  # ... except destructive tools


def test_failures_are_readable(env):
    env.manager.add({"nope": {"command": "definitely-not-a-real-program-xyz"}})
    entry = wait_for(env.manager, "nope", "failed")
    assert "not found" in entry["error"].lower() and entry["tool_count"] == 0
    env.manager.add({"crash": {"command": sys.executable, "args": ["-c", "import sys; sys.stderr.write('bad token\\n'); sys.exit(3)"]}})
    entry = wait_for(env.manager, "crash", "failed")
    assert entry["error"] and "bad token" in entry["error"]
    env.manager.add({"web": {"url": "http://127.0.0.1:9/mcp"}})
    assert wait_for(env.manager, "web", "failed")["error"]
    out = run_as(env, "run_command", {"command": "true"})  # other tools keep working
    assert out["returncode"] == 0


def test_enable_restart_remove_and_status_events(env):
    from jarvis.events import EventBus
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    env.manager.add_bus(bus)
    env.manager.add({"name": "echo", **echo_spec()})
    wait_for(env.manager, "echo", "connected")
    assert [e.data["status"] for e in seen if e.kind == "mcp_status"][-1] == "connected"
    env.manager.update("echo", enabled=False)
    assert wait_for(env.manager, "echo", "disabled")["tool_count"] == 0
    assert "mcp__echo__echo" not in env.registry.tool_names()
    assert json.loads((env.mind / "mcp.json").read_text())["mcpServers"]["echo"]["disabled"] is True
    env.manager.update("echo", enabled=True)
    wait_for(env.manager, "echo", "connected")
    env.manager.restart("echo")
    wait_for(env.manager, "echo", "connected")
    assert "mcp__echo__echo" in env.registry.tool_names()
    assert env.manager.remove("echo") and not env.manager.remove("echo")
    assert "mcp__echo__echo" not in env.registry.tool_names()
    assert json.loads((env.mind / "mcp.json").read_text())["mcpServers"] == {}


def test_manager_reads_existing_file_on_start(env):
    (env.mind / "mcp.json").write_text(json.dumps({"mcpServers": {
        "echo": echo_spec(), "off": echo_spec(disabled=True)}}))
    env.manager.start()
    wait_for(env.manager, "echo", "connected")
    assert next(s for s in env.manager.list() if s["name"] == "off")["status"] == "disabled"
    assert not any(n.startswith("mcp__off__") for n in env.registry.tool_names())


def test_web_routes(tmp_path, monkeypatch):
    from tests.test_webui import Env
    web = Env(tmp_path, monkeypatch)
    client = web.client
    try:
        assert client.get("/api/mcp/servers").json()["servers"] == []
        bad = client.post("/api/mcp/servers", json={"json": "{nope"})
        assert bad.status_code == 400 and "valid JSON" in bad.json()["error"]
        ok = client.post("/api/mcp/servers", json={"json": json.dumps({"name": "echo", **echo_spec()})})
        assert ok.status_code == 200 and ok.json()["added"] == ["echo"]
        manager = mcp_client.manager_for(web.jarvis.registry, web.jarvis.mind)
        wait_for(manager, "echo", "connected")
        listed = client.get("/api/mcp/servers").json()["servers"][0]
        assert listed["status"] == "connected" and listed["tool_count"] == 5 and listed["transport"] == "stdio"
        assert client.patch("/api/mcp/servers/echo", json={"trust": True}).json()["servers"][0]["trust"] is True
        wait_for(manager, "echo", "connected")
        assert client.post("/api/mcp/servers/echo/restart").status_code == 200
        wait_for(manager, "echo", "connected")
        assert client.patch("/api/mcp/servers/missing", json={}).status_code == 404
        assert client.delete("/api/mcp/servers/echo").json()["servers"] == []
        assert client.post("/api/mcp/servers", json={"mcpServers": {"x": {"command": "nope-nope"}}}).json()["added"] == ["x"]
    finally:
        manager = mcp_client.manager_for(web.jarvis.registry, web.jarvis.mind)
        manager.shutdown()
