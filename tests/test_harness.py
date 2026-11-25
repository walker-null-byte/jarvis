"""Harness upgrades: file tools, parallel tool calls, spilling, background commands, subagents."""

import json
import os
import threading
import time

import pytest

from jarvis import bg_commands, file_tools, subagents, toolctx
from jarvis.agent import Jarvis
from jarvis.config import DEFAULTS, NEW_CORE_TOOLS, OLD_CORE_TOOLS, load_config
from jarvis.events import EventBus
from jarvis.policy import CapabilityPolicy
from jarvis.providers.openai_chat import clean_messages
from tests.test_core_tools import ScriptedLLM, _calls, _config, _tool_call


def _agent(tmp_path, llm, bus=None, profile=None, **agent):
    config = _config(tmp_path, **agent)
    config["security"]["allowed_roots"] = [str(tmp_path)]
    jarvis = Jarvis(config, event_bus=bus, profile=profile)
    jarvis.llm = llm
    return jarvis


@pytest.fixture
def tools(tmp_path):
    policy = CapabilityPolicy({"security": {"allowed_roots": [str(tmp_path)]}})
    read_file, edit_file, grep, glob = file_tools.build_file_tools(policy)
    return type("T", (), dict(read=read_file, edit=edit_file, grep=grep, glob=glob, root=tmp_path))


# ---------- config ----------

def test_old_core_tools_default_is_upgraded(monkeypatch, tmp_path):
    from jarvis import config as cfg
    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    (tmp_path / "mind").mkdir()
    (tmp_path / "mind" / "runtime_config.json").write_text(json.dumps({"agent": {"core_tools": OLD_CORE_TOOLS}}))
    assert load_config()["agent"]["core_tools"] == NEW_CORE_TOOLS
    (tmp_path / "mind" / "runtime_config.json").write_text(json.dumps({"agent": {"core_tools": ["run_command"]}}))
    assert load_config()["agent"]["core_tools"] == ["run_command"]  # a customised list is kept
    assert DEFAULTS["agent"]["max_tool_rounds"] == 50 and DEFAULTS["agent"]["parallel_tools"] is True


# ---------- read_file / edit_file ----------

def test_read_file_numbers_lines_and_pages(tools):
    path = tools.root / "a.txt"
    path.write_text("".join(f"line {i}\n" for i in range(1, 101)))
    out = tools.read(str(path), offset=10, limit=3)
    assert out["content"].splitlines() == ["    10→line 10", "    11→line 11", "    12→line 12"]
    assert out["total_lines"] == 100 and out["truncated"] and out["next_offset"] == 13
    assert not tools.read(str(path))["truncated"]


def test_read_file_refuses_binary_missing_and_outside_roots(tools, tmp_path_factory):
    (tools.root / "b.bin").write_bytes(b"\x00\x01\x02")
    assert "binary" in tools.read(str(tools.root / "b.bin"))["error"]
    assert "error" in tools.read(str(tools.root / "nope.txt"))
    outside = tmp_path_factory.mktemp("outside") / "x.txt"
    outside.write_text("secret")
    assert tools.read(str(outside)).get("blocked")


def test_edit_file_exact_match_rules(tools):
    path = tools.root / "e.py"
    path.write_text("a = 1\nb = 2\nb = 2\n")
    assert "2 times" in tools.edit(str(path), "b = 2", "b = 3")["error"]
    assert tools.edit(str(path), "b = 2", "b = 3", replace_all=True)["replacements"] == 2
    assert path.read_text() == "a = 1\nb = 3\nb = 3\n"
    out = tools.edit(str(path), "a = 1", "a = 10")
    assert out["replacements"] == 1 and path.read_text().startswith("a = 10")
    missing = tools.edit(str(path), "  a = 10\nzzz", "x")["error"]
    assert "not found" in missing and "line(s) 1" in missing
    assert "identical" in tools.edit(str(path), "a = 10", "a = 10")["error"]
    assert "empty" in tools.edit(str(path), "", "x")["error"]
    assert tools.edit(str(tools.root / "new.txt"), "a", "b")["error"].startswith("Not a file")


# ---------- grep / glob ----------

def _tree(root):
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("import os\nTARGET = 1\n")
    (root / "src" / "b.txt").write_text("target again\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "skip.py").write_text("TARGET = 2\n")


@pytest.mark.parametrize("engine", ["rg", "python"])
def test_grep_modes(tools, monkeypatch, engine):
    if engine == "python":
        monkeypatch.setattr(file_tools.shutil, "which", lambda name: None)
    elif not file_tools.shutil.which("rg"):
        pytest.skip("ripgrep not installed")
    _tree(tools.root)
    out = tools.grep("TARGET", str(tools.root), ignore_case=True, glob="*.py")
    assert out["engine"] == engine
    assert "a.py" in out["output"] and "b.txt" not in out["output"]
    files = tools.grep("target", str(tools.root), ignore_case=True, output="files")
    assert {os.path.basename(f) for f in files["output"].splitlines()} >= {"a.py", "b.txt"}
    count = tools.grep("TARGET", str(tools.root / "src"), output="count")
    assert "a.py:1" in count["output"]
    ctx = tools.grep("TARGET", str(tools.root / "src" / "a.py"), context=1)
    assert "import os" in ctx["output"]
    assert tools.grep("TARGET", str(tools.root), max_results=1)["truncated"] in (True, False)
    assert "error" in tools.grep("(unclosed", str(tools.root))


def test_grep_and_glob_respect_roots(tools, tmp_path_factory):
    outside = tmp_path_factory.mktemp("o")
    assert tools.grep("x", str(outside)).get("blocked")
    assert tools.glob("*", str(outside)).get("blocked")


def test_glob_newest_first_and_skips_junk(tools):
    _tree(tools.root)
    old = tools.root / "src" / "a.py"
    os.utime(old, (1, 1))
    out = tools.glob("**/*.py", str(tools.root))
    assert out["matches"] == [os.path.join("src", "a.py")]  # node_modules skipped
    (tools.root / "src" / "c.py").write_text("")
    assert tools.glob("src/*.py", str(tools.root))["matches"][0].endswith("c.py")


# ---------- spill ----------

def test_spill_and_prune(tmp_path):
    big = "x" * 5000 + "MIDDLE" * 500 + "y" * 2500
    shown = file_tools.spill_output(tmp_path, "turn_1", 1, big)
    assert shown.startswith("x" * 5000) and "y" * 2000 in shown and "MIDDLE" not in shown
    saved = list((tmp_path / "tool_outputs").glob("*.txt"))
    assert len(saved) == 1 and saved[0].read_text() == big and str(saved[0]) in shown
    assert file_tools.spill_output(tmp_path, "t", 2, "short") == "short"
    os.utime(saved[0], (1, 1))
    assert file_tools.prune_spills(tmp_path) == 1 and not list((tmp_path / "tool_outputs").glob("*"))


def test_agent_spills_big_tool_results(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM(_calls(_tool_call("big", {}))))

    def big():
        """Returns a huge result."""
        return "z" * 20000

    jarvis.registry.register(big, name="big")
    try:
        jarvis.ask("go")
    finally:
        jarvis.registry.unregister("big")
    result = next(m["content"] for m in jarvis.history if m["role"] == "tool")
    assert len(result) < 9000 and "saved to" in result
    path = result.split("saved to ")[1].split(";")[0]
    assert open(path).read() == "z" * 20000


def test_read_file_result_is_not_spilled(tmp_path):
    (tmp_path / "big.txt").write_text("".join(f"line {i}\n" for i in range(3000)))
    jarvis = _agent(tmp_path, ScriptedLLM(_calls(_tool_call("read_file", {"path": str(tmp_path / "big.txt")}))))
    jarvis.ask("go")
    result = json.loads(next(m["content"] for m in jarvis.history if m["role"] == "tool"))
    assert result["truncated"] and result["next_offset"] > 1


# ---------- background commands ----------

def test_background_commands():
    started = bg_commands.JOBS.start("echo hello; sleep 30")
    job = started["job_id"]
    for _ in range(50):
        out = bg_commands.command_output(job)
        if "hello" in out["output"]:
            break
        time.sleep(0.1)
    assert out["status"] == "running" and "hello" in out["output"]
    killed = bg_commands.command_kill(job)
    assert killed["status"] == "killed"
    assert bg_commands.command_output(job)["status"] == "killed"
    assert "error" in bg_commands.command_output("job_999")


def test_run_command_background_flag_uses_policy(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    blocked = jarvis.registry.call("run_command", {"command": "rm -rf /tmp/x", "background": True})
    assert blocked.get("blocked")
    started = jarvis.registry.call("run_command", {"command": "sleep 20", "background": True})
    assert started["status"] == "running"
    bg_commands.command_kill(started["job_id"])


# ---------- parallel tool calls ----------

def _register_probe(jarvis, names, barrier=None, order=None):
    def make(name):
        def probe(delay: float = 0):
            """Probe tool."""
            if barrier:
                barrier.wait(timeout=5)
            if order is not None:
                order.append(name)
            time.sleep(delay)
            return f"{name} done"
        probe.read_only = True
        return probe
    for name in names:
        jarvis.registry.register(make(name), name=name)


def test_read_only_calls_run_concurrently_and_keep_order(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    llm = ScriptedLLM(_calls(*[_tool_call(n, {}, f"id_{n}") for n in ("p1", "p2", "p3")]))
    jarvis = _agent(tmp_path, llm, bus)
    _register_probe(jarvis, ["p1", "p2", "p3"], barrier=threading.Barrier(3))  # only passes if all 3 overlap
    try:
        jarvis.ask("go")
        tool_messages = [m for m in jarvis.history if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == ["id_p1", "id_p2", "id_p3"]
        assert [m["content"] for m in tool_messages] == ["p1 done", "p2 done", "p3 done"]
        started = [e for e in events if e.kind == "tool_started"]
        finished = [e for e in events if e.kind == "tool_finished"]
        assert {e.data["call_id"] for e in started} == {"id_p1", "id_p2", "id_p3"}
        assert {e.data["call_id"] for e in finished} == {"id_p1", "id_p2", "id_p3"}
        # the next request sees results in the original order
        sent = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in sent] == ["id_p1", "id_p2", "id_p3"]
    finally:
        for n in ("p1", "p2", "p3"):
            jarvis.registry.unregister(n)


def test_parallel_off_runs_sequentially(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM(_calls(_tool_call("q1", {}, "a"), _tool_call("q2", {}, "b"))),
                    parallel_tools=False)
    _register_probe(jarvis, ["q1", "q2"], barrier=threading.Barrier(2))
    barrier_timeout = []
    try:
        jarvis.ask("go")  # a barrier of 2 can never be passed sequentially
    except Exception as e:  # pragma: no cover
        barrier_timeout.append(e)
    results = [m["content"] for m in jarvis.history if m["role"] == "tool"]
    assert len(results) == 2 and all("Error" in r or "Broken" in r for r in results)
    for n in ("q1", "q2"):
        jarvis.registry.unregister(n)


def test_writes_run_in_order_between_parallel_runs(tmp_path):
    order = []
    jarvis = _agent(tmp_path, ScriptedLLM(_calls(*[_tool_call(n, {}, n) for n in ("r1", "r2", "w", "r3")])))
    _register_probe(jarvis, ["r1", "r2", "r3"], order=order)

    def w():
        """A write tool (not read-only)."""
        order.append("w")
        return "w done"

    jarvis.registry.register(w, name="w")
    try:
        jarvis.ask("go")
        assert order.index("w") == 2 and order[3] == "r3"  # w after r1,r2 (either order) and before r3
        assert [m["tool_call_id"] for m in jarvis.history if m["role"] == "tool"] == ["r1", "r2", "w", "r3"]
    finally:
        for n in ("r1", "r2", "r3", "w"):
            jarvis.registry.unregister(n)


def test_cancel_during_batch_answers_every_call(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM(_calls(_tool_call("c1", {}, "x1"), _tool_call("c2", {}, "x2"))))

    def c1():
        """Cancels the turn."""
        jarvis.cancel()
        return "ok"

    jarvis.registry.register(c1, name="c1")
    jarvis.registry.register(c1, name="c2")  # second sequential call: never runs
    try:
        jarvis.ask("go")
        assert [m["tool_call_id"] for m in jarvis.history if m["role"] == "tool"] == ["x1", "x2"]
        assert jarvis.history[-2]["content"] == "Cancelled."
    finally:
        jarvis.registry.unregister("c1")
        jarvis.registry.unregister("c2")


def test_budget_exhausted_status_and_default(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    llm = ScriptedLLM(*[_calls(_tool_call("get_current_time", {}, f"c{i}")) for i in range(3)])
    jarvis = _agent(tmp_path, llm, bus, max_tool_rounds=2)
    jarvis.ask("go")
    assert [e.data["status"] for e in events if e.kind == "turn_finished"] == ["budget_exhausted"]


# ---------- exposure ----------

def test_core_tools_and_hidden_legacy_web_tools(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    names = [t["function"]["name"] for t in jarvis.exposure.tools()]
    for name in NEW_CORE_TOOLS + ["spawn_agent"]:
        assert name in names
    hidden = jarvis.exposure.hidden_names()
    assert "web_search" in hidden and "fetch_page" in hidden
    assert "internet_search" not in hidden and "webpage_scrape" not in hidden
    assert "command_output" in hidden
    found = [t["name"] for t in jarvis.exposure.find_tools("search the web for a page", 8)["tools"]]
    assert "web_search" in found and "internet_search" not in found
    assert "error" in jarvis.exposure.run("internet_search", {"query": "x"})


# ---------- DeepSeek reasoning_content ----------

def test_deepseek_echoes_reasoning_only_for_current_turn():
    call = {"id": "1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "", "tool_calls": [call], "reasoning": "old thoughts"},
        {"role": "tool", "tool_call_id": "1", "content": "r"},
        {"role": "assistant", "content": "done", "reasoning": "more"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "", "tool_calls": [call], "reasoning": "fresh thoughts"},
        {"role": "tool", "tool_call_id": "1", "content": "r"},
    ]
    out = clean_messages(history, echo_reasoning=True)
    assert [m.get("reasoning_content") for m in out] == [None, None, None, None, None, "fresh thoughts", None]
    assert all("reasoning_content" not in m for m in clean_messages(history))


# ---------- subagents ----------

class _ChildLLM:
    """Stands in for jarvis.llm.LLM: every instance answers once with a fixed reply."""
    answer = "child answer"

    def __init__(self, config):
        pass

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        _ChildLLM.seen_tools = [t["function"]["name"] for t in tools or []]
        return {"role": "assistant", "content": _ChildLLM.answer}


def test_spawn_agent_runs_child_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.agent.LLM", _ChildLLM)
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    parent = _agent(tmp_path, ScriptedLLM(
        _calls(_tool_call("spawn_agent", {"task": "look around"}, "sp1"))), bus)
    parent.ask("delegate")
    result = json.loads(next(m["content"] for m in parent.history if m["role"] == "tool"))
    assert result["answer"] == "child answer" and "session_id" in result and result["tool_calls"] == 0
    assert "spawn_agent" not in _ChildLLM.seen_tools  # depth limit: hidden from the sub-agent
    meta = parent.sessions.get(result["session_id"])
    assert meta["kind"] == "subagent" and meta["owner"] == parent.session_id
    started = next(e for e in events if e.kind == "subagent_started")
    assert started.data["call_id"] == "sp1" and started.data["session_id"] == result["session_id"]


def test_spawn_agent_errors_and_answer_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.agent.LLM", _ChildLLM)
    monkeypatch.setattr(_ChildLLM, "answer", "a" * 9000)
    parent = _agent(tmp_path, ScriptedLLM())
    ctx = toolctx.ToolContext(parent.approvals, parent._cancel_event, parent, parent.session_id, "t", "c")
    with toolctx.tool_context(ctx):
        assert "Unknown agent" in subagents.spawn_agent("x", agent="no-such-plugin")["error"]
        assert "empty" in subagents.spawn_agent("  ")["error"]
        out = subagents.spawn_agent("x")
    assert len(out["answer"]) < 6100 and "cut" in out["answer"]
    assert "error" in subagents.spawn_agent("x")  # outside an agent turn


def test_spawn_agent_uses_plugin_profile_and_forbids_nesting(tmp_path, monkeypatch):
    from jarvis.runtime import AgentProfile
    monkeypatch.setattr("jarvis.agent.LLM", _ChildLLM)

    class Manager:
        def profile(self, agent_id):
            return AgentProfile(kind="agent", owner=agent_id, system_prompt="You find leads.", max_tool_rounds=99)

    subagents.set_agent_manager(Manager())
    try:
        parent = _agent(tmp_path, ScriptedLLM())
        ctx = toolctx.ToolContext(parent.approvals, parent._cancel_event, parent, parent.session_id, "t", "c")
        with toolctx.tool_context(ctx):
            profile = subagents.build_profile(parent, "lead-finder", "find")
            assert profile.kind == "subagent" and not profile.allow_spawn and profile.max_tool_rounds == 30
            assert "find leads" in profile.system_prompt and "sub-agent" in profile.system_prompt
            assert subagents.spawn_agent("find")["answer"] == "child answer"
        child_ctx = toolctx.ToolContext(parent.approvals, parent._cancel_event,
                                        Jarvis(parent.config, profile=profile), "s", "t", "c")
        with toolctx.tool_context(child_ctx):
            assert "cannot spawn" in subagents.spawn_agent("nested")["error"]
    finally:
        subagents.set_agent_manager(None)


def test_spawn_concurrency_cap(tmp_path, monkeypatch):
    live, peak, lock = [0], [0], threading.Lock()

    def fake_run(*args):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        with lock:
            live[0] -= 1
        return {"answer": "ok"}

    monkeypatch.setattr(subagents, "_run", fake_run)
    parent = _agent(tmp_path, ScriptedLLM())
    ctx = toolctx.ToolContext(parent.approvals, parent._cancel_event, parent, parent.session_id, "t", "c")

    def go():
        with toolctx.tool_context(ctx):
            subagents.spawn_agent("t")

    threads = [threading.Thread(target=go) for _ in range(7)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] == subagents.MAX_CONCURRENT


def test_glob_bare_name_searches_subfolders(tools):
    (tools.root / "web" / "js").mkdir(parents=True)
    (tools.root / "web" / "js" / "view_tools.js").write_text("x")
    assert tools.glob("view_*.js", str(tools.root))["matches"] == ["web/js/view_tools.js"]
