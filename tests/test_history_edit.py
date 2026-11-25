"""edit_message / regenerate / SessionStore.truncate_tail / semantic gating."""

import json

import pytest

from jarvis.events import EventBus
from jarvis.tool_exposure import enable_semantic_search, semantic_status
from tests.test_core_tools import ScriptedLLM, _agent, _calls, _config, _tool_call


def _reply(text):
    return {"role": "assistant", "content": text}


def _jsonl(jarvis):
    path = jarvis.sessions._messages_path(jarvis.session_id)
    return [json.loads(line)["message"] for line in path.read_text().splitlines()]


def _three_turns(tmp_path, bus=None):
    llm = ScriptedLLM(_reply("a1"), _reply("a2"), _reply("a3"), _reply("new answer"))
    jarvis = _agent(tmp_path, llm, bus=bus)
    for text in ("q1", "q2", "q3"):
        jarvis.ask(text)
    return jarvis, llm


def test_truncate_tail(tmp_path):
    jarvis, _ = _three_turns(tmp_path)
    store, sid = jarvis.sessions, jarvis.session_id
    assert store.truncate_tail(sid, 2) == 2
    assert [m["content"] for m in store.load(sid)] == ["q1", "a1", "q2", "a2"]
    assert store.get(sid)["message_count"] == 4
    assert store.truncate_tail(sid, 99) == 4 and store.load(sid) == []
    assert store.truncate_tail(sid, 0) == 0


def test_truncate_tail_pending_is_noop(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    assert jarvis.sessions.truncate_tail(jarvis.session_id, 3) == 0


def test_edit_middle_message(tmp_path):
    bus = EventBus()
    jarvis, llm = _three_turns(tmp_path, bus)
    seen = []
    bus.subscribe(lambda e: seen.append((e.kind, dict(e.data))))
    reply = jarvis.edit_message(2, "q2 edited")
    assert reply == "new answer"
    expected = ["q1", "a1", "q2 edited", "new answer"]
    assert [m["content"] for m in jarvis.history] == expected
    assert [m["content"] for m in _jsonl(jarvis)] == expected
    assert jarvis.sessions.get(jarvis.session_id)["message_count"] == 4
    # the model saw only the retained prefix plus the edited message
    sent = [m["content"] for m in llm.requests[-1]["messages"][1:]]
    assert sent == ["q1", "a1", "q2 edited"]
    kinds = [k for k, _ in seen]
    assert kinds.index("history_truncated") < kinds.index("user_message")
    assert dict(seen[kinds.index("history_truncated")][1])["length"] == 2


def test_edit_rejects_bad_targets(tmp_path):
    jarvis, _ = _three_turns(tmp_path)
    with pytest.raises(ValueError):
        jarvis.edit_message(1, "x")  # assistant message
    with pytest.raises(ValueError):
        jarvis.edit_message(99, "x")
    with pytest.raises(ValueError):
        jarvis.edit_message(0, "  ")
    assert len(jarvis.history) == 6


def test_regenerate_does_not_duplicate_user_message(tmp_path):
    bus = EventBus()
    jarvis, llm = _three_turns(tmp_path, bus)
    seen = []
    bus.subscribe(lambda e: seen.append(e.kind))
    assert jarvis.regenerate() == "new answer"
    assert [m["content"] for m in jarvis.history] == ["q1", "a1", "q2", "a2", "q3", "new answer"]
    assert [m["content"] for m in _jsonl(jarvis)] == [m["content"] for m in jarvis.history]
    assert [m["content"] for m in llm.requests[-1]["messages"][1:]][-1] == "q3"
    assert "history_truncated" in seen and "user_message" not in seen
    assert "turn_started" in seen and "turn_finished" in seen


def test_regenerate_drops_tool_messages_after_last_user(tmp_path):
    llm = ScriptedLLM(_calls(_tool_call("list_dir", {"path": "."})), _reply("listed"), _reply("again"))
    jarvis = _agent(tmp_path, llm)
    jarvis.ask("list")
    assert len(jarvis.history) == 4
    assert jarvis.regenerate() == "again"
    assert [m["role"] for m in jarvis.history] == ["user", "assistant"]
    assert len(_jsonl(jarvis)) == 2


def test_regenerate_without_user_message(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    with pytest.raises(ValueError):
        jarvis.regenerate()


def test_blocked_while_busy(tmp_path):
    jarvis, _ = _three_turns(tmp_path)
    jarvis._active_turn = "turn_x"
    with pytest.raises(RuntimeError, match="busy"):
        jarvis.edit_message(0, "x")
    with pytest.raises(RuntimeError, match="busy"):
        jarvis.regenerate()
    assert len(jarvis.history) == 6


def test_truncation_after_summarization(tmp_path):
    llm = ScriptedLLM(*[_reply(f"a{i}") for i in range(4)],
                      _reply("- summary"), *[_reply("a4"), _reply("redo")])
    jarvis = _agent(tmp_path, llm, history_char_limit=200, keep_recent=2)
    for i in range(4):
        jarvis.ask(f"question number {i} " + "x" * 80)
    jarvis.ask("final question")  # triggers summarization first
    assert jarvis.history[0]["content"].startswith("[Summary of the earlier conversation]")
    persisted_before = len(_jsonl(jarvis))
    assert persisted_before > len(jarvis.history)  # JSONL keeps the full log
    idx = len(jarvis.history) - 2  # the final user message
    assert jarvis.history[idx]["content"] == "final question"
    with pytest.raises(ValueError):
        jarvis.edit_message(0, "edit the summary")
    jarvis.edit_message(idx, "final edited")
    assert jarvis.history[idx]["content"] == "final edited"
    tail = _jsonl(jarvis)
    assert [m["content"] for m in tail[-2:]] == ["final edited", jarvis.history[-1]["content"]]
    assert tail[-3]["content"] != "final question" and len(tail) == persisted_before
    # regenerate keeps working on the summarized history
    jarvis.regenerate()
    assert jarvis.history[-2]["content"] == "final edited" and len(_jsonl(jarvis)) == persisted_before


# ---------- semantic gating ----------


class FakeRegistry:
    def __init__(self):
        self.enabled = 0

    def enable_semantic_search(self):
        self.enabled += 1


def test_semantic_off_by_config_even_if_cached(monkeypatch, tmp_path):
    monkeypatch.setattr("jarvis.tool_exposure.embedding_model_cached", lambda: True)
    config = _config(tmp_path)  # semantic_search False
    reg = FakeRegistry()
    assert enable_semantic_search(reg, config) is False and reg.enabled == 0
    assert semantic_status(config) == {"enabled": False, "model_cached": True}


def test_semantic_requires_cached_model(monkeypatch, tmp_path):
    config = _config(tmp_path, semantic_search=True)
    monkeypatch.setattr("jarvis.tool_exposure.embedding_model_cached", lambda: False)
    reg = FakeRegistry()
    assert enable_semantic_search(reg, config) is False and reg.enabled == 0
    assert semantic_status(config)["enabled"] is False
    monkeypatch.setattr("jarvis.tool_exposure.embedding_model_cached", lambda: True)
    assert enable_semantic_search(reg, config) is True and reg.enabled == 1
    assert semantic_status(config) == {"enabled": True, "model_cached": True}


def test_semantic_failure_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr("jarvis.tool_exposure.embedding_model_cached", lambda: True)

    class Broken:
        def enable_semantic_search(self):
            raise RuntimeError("boom")

    assert enable_semantic_search(Broken(), _config(tmp_path, semantic_search=True)) is False
