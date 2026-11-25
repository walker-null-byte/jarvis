"""Phase 3 tests: consolidation logic with a stubbed LLM. Run: python tests/test_consolidate.py"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.consolidate import _resolve_contradiction, consolidate
from jarvis.memory import FactStore


def test_consolidate_no_episodes():
    with tempfile.TemporaryDirectory() as tmp:
        mind = Path(tmp) / "mind"
        mind.mkdir()
        (mind / "episodes").mkdir()
        (mind / "facts").mkdir()
        (mind / "skills").mkdir()

        class FakeClient:
            def chat(self, **kwargs):
                raise AssertionError("LLM should not be called with no episodes")

        class FakeConfig:
            def __getitem__(self, k):
                return {"llm": {"base_url": "x", "model": "x", "api_key_env": "x"}}.get(k, {})

        # Should not crash and should return zeros
        result = consolidate(mind=mind, config=FakeConfig())
        assert result == {"facts_added": 0, "facts_updated": 0, "contradictions_resolved": 0,
                          "skills_drafted": 0, "facts_decayed": 0, "facts_archived": 0}
        print("PASS test_consolidate_no_episodes")


def test_consolidate_distills_facts():
    import datetime
    with tempfile.TemporaryDirectory() as tmp:
        mind = Path(tmp) / "mind"
        mind.mkdir()
        (mind / "episodes").mkdir()
        (mind / "facts").mkdir()
        (mind / "skills").mkdir()

        # Write a fake episode for TODAY
        today = datetime.date.today()
        (mind / "episodes" / f"{today:%Y-%m-%d}.md").write_text("""
## 10:00:00 — user
remember i prefer short answers

## 10:00:01 — tool: remember
result: {"saved": "user-prefers-short-answers", "updated": false, "strength": 1.0}

## 10:05:00 — user
run df -h / to check disk space

## 10:05:01 — tool: run_command
result: {"returncode": 0, "stdout": "Filesystem  Size  Used Avail Use% Mounted on\n/dev/nvme0n1p4  256G  231G  24G  91% /"}
""")

        class FakeCompletions:
            def create(self, model, max_tokens, messages):
                prompt = messages[0]["content"]
                if "Extract" in prompt or "durable facts" in prompt:
                    class Choice:
                        message = type("M", (), {"content": json.dumps([{
                            "fact": "The user prefers short, direct answers.",
                            "why": "Explicitly stated as a preference.",
                            "name": "user-prefers-short-answers"
                        }])})()
                    return type("R", (), {"choices": [Choice()]})()
                elif "contradict" in prompt.lower():
                    class Choice:
                        message = type("M", (), {"content": "[]"})()
                    return type("R", (), {"choices": [Choice()]})()
                elif "skill" in prompt.lower():
                    class Choice:
                        message = type("M", (), {"content": "[]"})()
                    return type("R", (), {"choices": [Choice()]})()
                raise AssertionError(f"Unexpected prompt: {prompt[:100]}")

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        class FakeConfig:
            def __getitem__(self, k):
                return {"llm": {"base_url": "x", "model": "x", "api_key_env": "x"}}.get(k, {})

        result = consolidate(mind=mind, config=FakeConfig(), model_override="x", client=FakeClient())
        assert result["facts_added"] >= 1, result

        # Verify the fact was written
        facts = list((mind / "facts").glob("*.md"))
        assert len(facts) >= 1
        content = facts[0].read_text()
        assert "short" in content.lower()
        print("PASS test_consolidate_distills_facts")


def test_decay_and_archive():
    with tempfile.TemporaryDirectory() as tmp:
        mind = Path(tmp) / "mind"
        mind.mkdir()
        (mind / "episodes").mkdir()
        (mind / "facts").mkdir()
        (mind / "skills").mkdir()

        store = FactStore(mind)
        # Create an old weak fact
        old_path = store.dir / "old-fact.md"
        store._write(old_path, {
            "created": "2026-05-01",
            "strength": "0.2",
            "recalls": "0",
            "last_recalled": "2026-05-01",
        }, "This is an old weak fact.\n\nWhy: ancient history.")

        class FakeClient:
            def chat(self, **kwargs):
                class Choice:
                    message = type("M", (), {"content": "[]"})()
                return type("R", (), {"choices": [Choice()]})()

        class FakeConfig:
            def __getitem__(self, k):
                return {"llm": {"base_url": "x", "model": "x", "api_key_env": "x"}}.get(k, {})

        result = consolidate(mind=mind, config=FakeConfig(), model_override="x")
        assert result["facts_archived"] >= 1, result
        archive_dir = store.dir / "archive"
        assert archive_dir.exists()
        archived = list(archive_dir.glob("*.md"))
        assert len(archived) >= 1
        print("PASS test_decay_and_archive")


def test_contradiction_resolution_is_idempotent_and_recoverable():
    with tempfile.TemporaryDirectory() as tmp:
        mind = Path(tmp) / "mind"
        (mind / "facts").mkdir(parents=True)
        store = FactStore(mind)
        store.remember("The user prefers dark mode.", name="theme-a")
        store.remember("The user prefers light mode.", name="theme-b")

        contradiction = {"a": "theme-a", "b": "theme-b", "keep": "a", "resolution": "a is current"}
        assert _resolve_contradiction(store, contradiction) is True
        assert _resolve_contradiction(store, contradiction) is False
        assert (store.dir / "theme-a.md").exists()
        assert (store.dir / "archive" / "theme-b.md").exists()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
