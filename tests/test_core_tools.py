"""Tool exposure (search mode), call_tool proxying, skills, persistence."""

import copy
import json

from jarvis.agent import Jarvis
from jarvis.config import DEFAULTS
from jarvis.events import EventBus
from jarvis.sessions import SessionStore


def _config(tmp_path, **agent):
    config = copy.deepcopy(DEFAULTS)
    config["paths"]["mind"] = str(tmp_path / "mind")
    config["voice"]["enabled"] = False
    config["agent"]["semantic_search"] = False
    config["agent"].update(agent)
    return config


def _tool_call(name, args, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM:
    """Returns scripted assistant messages and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat(self, messages, tools=None, on_text=None, on_reasoning=None):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        reply = self.replies.pop(0) if self.replies else {"role": "assistant", "content": "done"}
        if on_reasoning and reply.get("reasoning"):
            on_reasoning(reply["reasoning"])
        return reply


def _agent(tmp_path, llm, bus=None, **agent):
    jarvis = Jarvis(_config(tmp_path, **agent), event_bus=bus)
    jarvis.llm = llm
    return jarvis


def _names(tools):
    return [t["function"]["name"] for t in tools]


def _calls(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def test_search_mode_sends_fixed_small_tools_array(tmp_path):
    llm = ScriptedLLM(
        _calls(_tool_call("list_dir", {"path": "."})),
        _calls(_tool_call("list_dir", {"path": "."}, "c2")),
    )
    jarvis = _agent(tmp_path, llm)
    jarvis.ask("first")
    jarvis.ask("second")
    arrays = [json.dumps(r["tools"]) for r in llm.requests if r["tools"]]
    assert len(set(arrays)) == 1
    names = _names(llm.requests[0]["tools"])
    assert names[-3:] == ["find_tools", "call_tool", "read_skill"]
    assert names[:-3] == sorted(names[:-3])
    assert "cron_add" not in names and "run_command" in names


def test_all_mode_sends_every_tool(tmp_path):
    llm = ScriptedLLM()
    jarvis = _agent(tmp_path, llm, tool_mode="all")
    jarvis.ask("hi")
    names = _names(llm.requests[0]["tools"])
    assert "cron_add" in names and "find_tools" not in names
    assert "# Tools" not in llm.requests[0]["messages"][0]["content"]


def test_find_tools_excludes_core_and_recovers(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    found = jarvis.exposure.find_tools("cron schedule job", 5)
    names = [t["name"] for t in found["tools"]]
    assert names and all(n.startswith("cron_") for n in names)
    assert "input_schema" in found["tools"][0]
    assert not {"run_command", "read_file", "find_tools"} & set(names)
    nothing = jarvis.exposure.find_tools("zzzqqq", 5)
    assert nothing["tools"] == [] and nothing["available"]
    assert "run_command" not in [e["name"] for e in nothing["available"]]


def test_call_tool_dispatch_events_and_errors(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    seen = []
    llm = ScriptedLLM(
        _calls(_tool_call("call_tool", {"name": "cron_list", "arguments": {}}, "a")),
        _calls(_tool_call("call_tool", {"name": "cron_lst", "arguments": {}}, "b")),
        _calls(_tool_call("call_tool", {"name": "find_tools", "arguments": {"query": "x"}}, "c")),
    )
    jarvis = _agent(tmp_path, llm, bus)
    jarvis.ask("go", on_tool=lambda name, args: seen.append(name))
    assert seen[0] == "cron_list"
    started = [e for e in events if e.kind == "tool_started"]
    finished = [e for e in events if e.kind == "tool_finished"]
    assert started[0].data["name"] == "cron_list" and started[0].data["via"] == "call_tool"
    assert finished[0].data["via"] == "call_tool"
    tool_results = [m["content"] for m in jarvis.history if m["role"] == "tool"]
    assert "Unknown tool" in tool_results[1] and "cron_list" in tool_results[1]
    assert "directly callable" in tool_results[2]


def test_read_skill_rejects_traversal(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    (jarvis.mind / "skills" / "backup.md").write_text("# Backup files\n## Steps\n- zip\n")
    (jarvis.mind / "secret.md").write_text("nope")
    assert "Backup files" in jarvis.exposure.read_skill("backup")
    for bad in ("../secret", "..", "a/b", "", "Backup"):
        assert "error" in jarvis.exposure.read_skill(bad)
    assert "error" in jarvis.exposure.read_skill("missing")


def test_tools_array_rebuilt_only_when_registry_changes(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    before = jarvis.exposure.tools()
    jarvis.exposure.refresh()
    assert jarvis.exposure.tools() is before

    def extra_tool():
        """Do extra."""
        return "x"

    jarvis.registry.register(extra_tool)
    try:
        jarvis.exposure.refresh()
        assert "extra_tool" in jarvis.exposure.prompt_section()
        assert _names(jarvis.exposure.tools()) == _names(before)  # non-core stays hidden
    finally:
        jarvis.registry.unregister("extra_tool")


def test_reasoning_and_provider_state_not_persisted(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    reply = {"role": "assistant", "content": "ok", "reasoning": "thinking...",
             "provider_state": {"backend": "x", "model": "m", "items": [1]}}
    jarvis = _agent(tmp_path, ScriptedLLM(reply), bus)
    streamed = []
    jarvis.ask("hi", on_reasoning=streamed.append)
    assert streamed == ["thinking..."]
    assert any(e.kind == "assistant_reasoning_delta" for e in events)
    assert "provider_state" in jarvis.history[-1]  # kept in memory
    saved = jarvis.sessions.load(jarvis.session_id)[-1]
    assert "provider_state" not in saved and "reasoning" not in saved
    raw = (jarvis.mind / "sessions" / f"{jarvis.session_id}.jsonl").read_text()
    assert "provider_state" not in raw and "thinking" not in raw


def test_summarize_strips_provider_state_from_recent(tmp_path):
    llm = ScriptedLLM({"role": "assistant", "content": "- summary"})
    jarvis = _agent(tmp_path, llm, history_char_limit=300, keep_recent=2)
    for i in range(6):
        jarvis.history.append({"role": "user", "content": f"q{i} " + "x" * 100})
        jarvis.history.append({"role": "assistant", "content": f"a{i}",
                               "provider_state": {"backend": "x", "model": "m", "items": []}})
    jarvis._maybe_summarize()
    assert jarvis.history[0]["content"].startswith("[Summary")
    assert all("provider_state" not in m for m in jarvis.history)


def test_session_character_persists_across_resume(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    events = []
    jarvis.events.subscribe(events.append)
    assert jarvis.set_character("nonexistent-card")["error"]
    result = jarvis.set_character("casual")
    assert result["character"]["id"] == "casual"
    assert any(e.kind == "character_changed" for e in events)
    old = jarvis.session_id
    assert jarvis.sessions.get(old)["character"] == "casual"
    # Empty sessions stay in memory; the first message persists them (with the card).
    assert SessionStore(jarvis.mind).get(old) is None
    jarvis.sessions.append(old, {"role": "user", "content": "hi"})
    assert SessionStore(jarvis.mind).get(old)["character"] == "casual"
    jarvis.new_session()
    assert jarvis.character == "jarvis"
    jarvis.resume_session(old)
    assert jarvis.character == "casual"
    assert Jarvis(jarvis.config, session_id=old).character == "casual"
    assert jarvis.busy is False
