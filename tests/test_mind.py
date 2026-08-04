"""Phase 2 tests: FactStore + history summarization. Run: python tests/test_mind.py"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.memory import FactStore
from jarvis.mind_tools import build_mind_tools


def test_fact_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        store = FactStore(tmp)

        r = store.remember("The user prefers dark mode everywhere.", why="said so directly")
        assert r["saved"] == "the-user-prefers-dark-mode-everywhere"
        assert r["updated"] is False and r["strength"] == 1.0

        # index regenerated
        index = (Path(tmp) / "MEMORY.md").read_text()
        assert "the-user-prefers-dark-mode-everywhere" in index

        # recall finds it and reinforces
        hits = store.recall("what theme does the user like? dark?")
        assert hits[0]["name"] == "the-user-prefers-dark-mode-everywhere"
        assert hits[0]["strength"] == 1.25
        assert "dark mode" in hits[0]["memory"]

        # correcting under the same name updates + reinforces
        r2 = store.remember("The user prefers dark mode, except in PDFs.",
                            name="the-user-prefers-dark-mode-everywhere")
        assert r2["updated"] is True and r2["strength"] > 1.25

        # forget removes it from store and index
        assert store.forget("the-user-prefers-dark-mode-everywhere") == {
            "forgotten": "the-user-prefers-dark-mode-everywhere"
        }
        assert "dark-mode" not in (Path(tmp) / "MEMORY.md").read_text()
        assert "error" in store.forget("nope")

        assert store.recall("dark mode") == {"found": 0, "note": "No matching memories."}


def test_fact_provenance_and_safe_atomic_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        store = FactStore(tmp)
        result = store.remember(
            "The user prefers concise answers.",
            why="stated directly",
            name="answer-style",
            source="direct",
            source_episode="2026-08-01",
            source_event_id="turn_123",
            confidence=0.94,
            category="preference",
            observed_at="2026-08-01T10:00:00",
            valid_from="2026-08-01",
            correction_of="old-answer-style",
            provenance="User instruction",
        )
        assert result["confidence"] == 0.94
        data = store._read(Path(tmp) / "facts" / "answer-style.md")
        assert data["meta"]["source_episode"] == "2026-08-01"
        assert data["meta"]["source_event_id"] == "turn_123"
        assert data["meta"]["category"] == "preference"
        assert data["meta"]["correction_of"] == "old-answer-style"
        assert not list((Path(tmp) / "facts").glob("*.tmp"))

        hit = store.recall("concise answers")[0]
        assert hit["category"] == "preference"
        assert hit["source"] == "direct"
        assert hit["source_episode"] == "2026-08-01"

        assert "Invalid memory id" in store.forget("../outside")["error"]


def test_mind_tools_register():
    from turbomcp import TurboMCP

    with tempfile.TemporaryDirectory() as tmp:
        reg = TurboMCP()
        for f in build_mind_tools(tmp):
            reg.register(f)
        assert {
            "remember", "recall", "forget", "memory_review", "memory_review_decide",
            "memory_expire", "memory_never_remember", "memory_allow_remember", "memory_rules",
        } == set(reg.list_tools())
        schema = reg.tools["remember"]["input_schema"]
        assert schema["required"] == ["fact"]
        out = reg.call("remember", {"fact": "test fact", "why": "testing"})
        assert out["saved"] == "test-fact"


def test_memory_review_expiry_and_privacy_rules():
    with tempfile.TemporaryDirectory() as tmp:
        store = FactStore(tmp)
        store.remember(
            "A temporary project preference.",
            name="temporary-preference",
            source="consolidation",
            review_status="pending",
        )
        queue = store.review_memories()
        assert queue["count"] == 1
        assert queue["memories"][0]["status"] == "pending"

        approved = store.set_review("temporary-preference", "approve", note="verified")
        assert approved["status"] == "approved"
        assert store.review_memories()["count"] == 0

        expired = store.expire("temporary-preference", "2020-01-01")
        assert expired["valid_until"] == "2020-01-01"
        assert store.recall("temporary project") == {"found": 0, "note": "No matching memories."}
        assert store.review_memories(include_approved=True)["memories"][0]["expired"] is True

        store.add_never_remember("passport number")
        blocked = store.remember("The passport number is 123", name="private-id")
        assert blocked["blocked"] is True
        assert not (Path(tmp) / "facts" / "private-id.md").exists()
        assert store.memory_rules()["never_remember"] == ["passport number"]
        store.remove_never_remember("passport number")
        assert store.remember("The passport number is 123", name="private-id")["saved"] == "private-id"


def test_summarization():
    # Build a Jarvis with a stubbed LLM and a tiny history limit.
    os.environ.setdefault("JARVIS_API_KEY", "x")
    from jarvis.agent import Jarvis
    from jarvis.config import load_config

    config = load_config()
    config["agent"]["history_char_limit"] = 500
    config["agent"]["keep_recent"] = 4

    jarvis = Jarvis(config)

    class FakeLLM:
        def chat(self, messages, tools=None, on_text=None):
            assert "Transcript:" in messages[0]["content"]
            return {"role": "assistant", "content": "- user is testing\n- all fine"}

    jarvis.llm = FakeLLM()

    # Fabricate a long alternating history.
    for i in range(10):
        jarvis.history.append({"role": "user", "content": f"question {i} " + "x" * 80})
        jarvis.history.append({"role": "assistant", "content": f"answer {i} " + "y" * 80})

    jarvis._maybe_summarize()

    assert jarvis.history[0]["role"] == "user"
    assert jarvis.history[0]["content"].startswith("[Summary of the earlier conversation]")
    assert "user is testing" in jarvis.history[0]["content"]
    assert jarvis.history[1] == {"role": "assistant", "content": "Understood — context noted."}
    # recent messages kept verbatim, cut lands on a user message
    assert jarvis.history[2]["role"] == "user"
    assert jarvis.history[-1]["content"].startswith("answer 9")
    assert len(jarvis.history) <= 2 + 5

    # under the limit -> untouched
    before = list(jarvis.history)
    jarvis._maybe_summarize()
    assert jarvis.history == before


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
