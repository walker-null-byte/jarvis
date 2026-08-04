import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from textual.widgets import Input, OptionList, Static

from jarvis import jobs
from jarvis.agent import Jarvis
from jarvis.artifacts import ArtifactStore
from jarvis.codify import _parse_skill, _validate_generated_code
from jarvis.commands import (
    _split_credentials,
    dialog_for_command,
    handle_slash_command,
    slash_command_suggestions,
)
from jarvis.config import DEFAULTS, load_config, runtime_config_path
from jarvis.events import EventBus
from jarvis.llm import LLM, LLMError
from jarvis.memory import FactStore
from jarvis.redaction import redact_text
from jarvis.registry import ToolRegistry
from jarvis.sessions import SessionStore
from jarvis.supervisor import log_crash
from jarvis.tools_core import _run_command, build_registry
from jarvis.ui import JarvisUI, ProviderScreen, SlashFormScreen, Waveform
from jarvis.voice import NullVoice, get_voice


def _temp_config(tmp_path):
    config = copy.deepcopy(DEFAULTS)
    config["paths"]["mind"] = str(tmp_path / "mind")
    config["paths"]["models"] = str(tmp_path / "models")
    config["voice"]["enabled"] = False
    return config


def _fake_test_connection(jarvis, base_url=None, api_key_env=None, api_key=None, timeout=5.0, check_model=True):
    url = base_url or jarvis.config["llm"].get("base_url") or "http://example.test/v1"
    return True, "Connected — 2 model(s) listed", ["a", "b"], url


def test_sessions_and_artifacts_are_persistent(tmp_path):
    mind = tmp_path / "mind"
    sessions = SessionStore(mind)
    session_id = sessions.create()
    sessions.append(session_id, {"role": "user", "content": "show me a chart"})
    assert sessions.load(session_id)[0]["content"] == "show me a chart"
    assert sessions.get(session_id)["message_count"] == 1

    store = ArtifactStore(mind)
    created = store.create("chart", "Disk usage", data={"x": [1, 2], "y": [3, 4]})
    artifact_id = created["artifact_id"]
    assert store.get(artifact_id)["title"] == "Disk usage"
    assert store.update(artifact_id, title="Updated") ["artifact"]["title"] == "Updated"
    assert store.close(artifact_id)["status"] == "closed"


def test_sessions_can_search_and_export(tmp_path):
    sessions = SessionStore(tmp_path / "mind")
    session_id = sessions.create()
    sessions.append(session_id, {"role": "user", "content": "Analyze the sales chart"})
    sessions.append(session_id, {"role": "assistant", "content": "The trend is upward."})

    assert sessions.search("sales")[0]["id"] == session_id
    assert sessions.search("missing") == []
    markdown = sessions.export(session_id)
    assert "# Analyze the sales chart" in markdown
    assert "The trend is upward." in markdown
    payload = json.loads(sessions.export(session_id, format="json"))
    assert payload["session"]["id"] == session_id
    assert len(payload["messages"]) == 2

    target = tmp_path / "exports" / "session.md"
    assert sessions.export(session_id, destination=target) == str(target)
    assert target.exists()


def test_voice_disabled_is_a_noop():
    config = copy.deepcopy(DEFAULTS)
    config["voice"]["enabled"] = False
    assert isinstance(get_voice(config), NullVoice)


def test_slash_commands_change_runtime_and_persist_selected_settings(tmp_path):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    try:
        assert handle_slash_command(jarvis, "hello") is None
        assert "Slash commands" in handle_slash_command(jarvis, "/help")

        assert "Model changed" in handle_slash_command(jarvis, "/model set local-model")
        assert jarvis.config["llm"]["model"] == "local-model"
        assert jarvis.llm.model == "local-model"

        assert "ollama" in handle_slash_command(jarvis, "/provider use ollama")
        assert jarvis.config["llm"]["provider"] == "ollama"
        assert jarvis.config["llm"]["base_url"] == "http://localhost:11434/v1"

        assert "Set llm.timeout" in handle_slash_command(
            jarvis, "/settings set llm.timeout 45"
        )
        assert jarvis.config["llm"]["timeout"] == 45
        assert handle_slash_command(jarvis, "/settings get llm.timeout") == "llm.timeout = 45"

        saved = handle_slash_command(jarvis, "/settings save")
        assert str(runtime_config_path(config)) in saved
        persisted = json.loads(runtime_config_path(config).read_text())
        assert persisted["llm"]["model"] == "local-model"
        assert persisted["llm"]["timeout"] == 45
        assert "secret" not in json.dumps(persisted).lower()
    finally:
        jarvis.registry.stop_watcher()


def test_slash_command_suggestions_filter_top_level_and_subcommands():
    assert [item["command"] for item in slash_command_suggestions("/")] == [
        "/help", "/model", "/provider", "/settings", "/status"
    ]
    assert [item["command"] for item in slash_command_suggestions("/mod")] == ["/model"]
    assert [item["command"] for item in slash_command_suggestions("/model ")] == [
        "/model list"
    ]
    assert [item["command"] for item in slash_command_suggestions("/provider ")] == [
        "/provider list", "/provider use", "/provider set", "/provider test",
        "/provider models",
    ]
    assert slash_command_suggestions("/provider use ") == []
    assert [item["command"] for item in slash_command_suggestions("/settings se")] == [
        "/settings set"
    ]
    assert slash_command_suggestions("please /model") == []


def test_dialog_specs_cover_leaf_commands_that_need_input(tmp_path):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    try:
        assert dialog_for_command(jarvis, "hello") is None
        assert dialog_for_command(jarvis, "/model") is not None
        assert dialog_for_command(jarvis, "/model gemma4") is None
        assert dialog_for_command(jarvis, "/provider") is not None
        assert dialog_for_command(jarvis, "/provider list") is None
        assert dialog_for_command(jarvis, "/provider use") is not None
        assert dialog_for_command(jarvis, "/provider use ollama") is None
        assert dialog_for_command(jarvis, "/provider set") is not None
        assert dialog_for_command(jarvis, "/provider test") is None
        assert dialog_for_command(jarvis, "/provider models") is None
        assert dialog_for_command(jarvis, "/settings") is None
        assert dialog_for_command(jarvis, "/settings get") is not None
        assert dialog_for_command(jarvis, "/settings set llm.timeout 45") is None
        assert dialog_for_command(jarvis, "/settings save") is None
        assert dialog_for_command(jarvis, "/status") is None

        model = dialog_for_command(jarvis, "/model")
        assert model["command"]({"model": "my-model"}) == "/model set my-model"

        # Provider management lives in one place: the provider manager screen.
        for command in ("/provider", "/provider use", "/provider set"):
            spec = dialog_for_command(jarvis, command)
            assert spec["screen"] == "provider"

        change = dialog_for_command(jarvis, "/settings set")
        assert change["command"]({"path": "llm.timeout", "value": "45"}) == (
            "/settings set llm.timeout 45"
        )
        assert len(change["fields"][0]["options"]) >= 20
    finally:
        jarvis.registry.stop_watcher()


def test_provider_set_auto_fixes_missing_v1_and_tests_connection(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    try:
        class FakeLLM:
            def __init__(self, probe_config):
                self.base_url = probe_config["llm"]["base_url"]

            def probe(self, timeout=5.0):
                if self.base_url.rstrip("/") == "http://example.test:6446":
                    return False, "HTTP 404: Cannot POST /chat/completions", [], 404
                return True, "Connected — 2 model(s) listed", ["a", "b"], 200

        monkeypatch.setattr("jarvis.commands.LLM", FakeLLM)
        jarvis.llm.probe = lambda timeout=5.0: (
            False, "HTTP 404: Cannot POST /chat/completions", [], 404
        )
        result = handle_slash_command(jarvis, "/provider set http://example.test:6446")
        assert jarvis.config["llm"]["base_url"] == "http://example.test:6446/v1"
        assert "OK — Connected — 2 model(s) listed" in result
    finally:
        jarvis.registry.stop_watcher()


def test_provider_test_models_and_status_qol(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    try:
        jarvis.llm.probe = lambda timeout=5.0: (True, "Connected — 3 model(s) listed", ["a", "b", "c"], 200)
        assert "OK — Connected — 3 model(s) listed" in handle_slash_command(jarvis, "/provider test")
        assert "Testing ollama" in handle_slash_command(jarvis, "/provider test ollama")
        listed = handle_slash_command(jarvis, "/provider models")
        assert listed.startswith("Models:") and "a\nb\nc" in listed
        assert handle_slash_command(jarvis, "/model list") == listed
        assert "Run /provider test" in handle_slash_command(jarvis, "/status")

        jarvis.llm.probe = lambda timeout=5.0: (False, "HTTP 401: Invalid API key", [], 401)
        failed = handle_slash_command(jarvis, "/provider test")
        assert "Failed — HTTP 401" in failed
        assert "Could not list models" in handle_slash_command(jarvis, "/model list")
    finally:
        jarvis.registry.stop_watcher()


def test_split_credentials_distinguishes_env_var_from_literal(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "sk-env")
    assert _split_credentials("") == (None, None)
    assert _split_credentials("JARVIS_API_KEY") == ("JARVIS_API_KEY", None)
    literal = "oc-1b50289171f4709611efefc0613603ae3ecc6355"
    assert _split_credentials(literal) == (None, literal)


def test_provider_set_accepts_literal_key_or_env_var(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    try:
        jarvis.llm.probe = lambda timeout=5.0: (
            True, "Connected — 2 model(s) listed", ["a", "b"], 200
        )
        literal = "oc-1b50289171f4709611efefc0613603ae3ecc6355"
        handle_slash_command(
            jarvis, f"/provider set http://example.test:6446 {literal}"
        )
        assert jarvis.config["llm"]["api_key"] == literal
        assert jarvis.config["llm"]["api_key_env"] is None

        saved = handle_slash_command(jarvis, "/settings save")
        assert str(runtime_config_path(config)) in saved
        assert literal not in runtime_config_path(config).read_text()
        assert "API key: literal (session-local)" in handle_slash_command(jarvis, "/status")

        monkeypatch.setenv("MY_KEY", "sk-secret")
        handle_slash_command(jarvis, "/provider set http://example.test:6446 MY_KEY")
        assert jarvis.config["llm"]["api_key_env"] == "MY_KEY"
        assert jarvis.config["llm"]["api_key"] is None
        assert "API key: env MY_KEY" in handle_slash_command(jarvis, "/status")

        handle_slash_command(jarvis, "/provider use ollama")
        assert jarvis.config["llm"]["api_key"] is None
        assert jarvis.config["llm"]["api_key_env"] == "JARVIS_API_KEY"
    finally:
        jarvis.registry.stop_watcher()


def test_llm_resolves_literal_key_before_env_var(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-secret")
    config = copy.deepcopy(DEFAULTS)
    config["llm"]["api_key"] = "oc-29a53b895a30f1409304ad21b24fdce115b7ff29"
    config["llm"]["api_key_env"] = "MY_KEY"
    llm = LLM(config)
    assert llm.api_key == "oc-29a53b895a30f1409304ad21b24fdce115b7ff29"

    config["llm"]["api_key"] = None
    llm = LLM(config)
    assert llm.api_key == "sk-secret"


def test_provider_use_switches_and_restores_models_per_provider(tmp_path):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    try:
        jarvis.llm.probe = lambda timeout=5.0: (
            True, "Connected — 2 model(s) listed", ["llama3", "gemma"], 200
        )
        handle_slash_command(jarvis, "/model set llama3")
        handle_slash_command(jarvis, "/provider use ollama")
        assert jarvis.config["llm"]["provider"] == "ollama"
        assert jarvis.config["llm"]["model"] == "llama3"

        handle_slash_command(jarvis, "/model set gemma")
        handle_slash_command(jarvis, "/provider set http://example.test:6446")
        assert jarvis.config["llm"]["provider"] == "custom"
        assert jarvis.config["llm"]["model"] == "llama3"

        handle_slash_command(jarvis, "/provider use ollama")
        assert jarvis.config["llm"]["model"] == "gemma"

        memory = json.loads(
            (tmp_path / "mind" / "provider_models.json").read_text()
        )
        assert memory == {"custom": "llama3", "ollama": "gemma"}
    finally:
        jarvis.registry.stop_watcher()


def test_provider_and_model_changes_persist_automatically(tmp_path):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    try:
        jarvis.llm.probe = lambda timeout=5.0: (
            True, "Connected — 2 model(s) listed", ["a", "b"], 200
        )
        result = handle_slash_command(
            jarvis, "/provider set http://example.test:6446"
        )
        assert "Settings saved" in result
        handle_slash_command(jarvis, "/model set gemma2")
        overrides = json.loads(runtime_config_path(config).read_text())
        assert overrides["llm"]["provider"] == "custom"
        assert overrides["llm"]["base_url"] == "http://example.test:6446"
        assert overrides["llm"]["model"] == "gemma2"
    finally:
        jarvis.registry.stop_watcher()


def test_agent_emits_events_and_persists_messages(tmp_path):
    config = _temp_config(tmp_path)
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    jarvis = Jarvis(config, event_bus=bus)

    class FakeLLM:
        def chat(self, messages, tools=None, on_text=None):
            if on_text:
                on_text("Acknowledged.")
            return {"role": "assistant", "content": "Acknowledged."}

    jarvis.llm = FakeLLM()
    assert jarvis.ask("hello") == "Acknowledged."
    kinds = [event.kind for event in events]
    assert "user_message" in kinds
    assert "assistant_delta" in kinds
    assert "turn_finished" in kinds
    assert jarvis.sessions.load(jarvis.session_id)[-1]["content"] == "Acknowledged."
    jarvis.registry.stop_watcher()


def test_environment_config_override(monkeypatch):
    monkeypatch.setenv("JARVIS_AGENT_MAX_TOOL_ROUNDS", "7")
    monkeypatch.setenv("JARVIS_VOICE_ENABLED", "true")
    config = load_config()
    assert config["agent"]["max_tool_rounds"] == 7
    assert config["voice"]["enabled"] is True


def test_jobs_validate_and_deduplicate(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    crontab = tmp_path / "jarvis-crontab"
    monkeypatch.setattr(jobs, "_crontab_file", lambda *args: crontab)
    assert "error" in jobs.cron_add("bad/name", "* * * * *", "run", config)
    assert "error" in jobs.cron_add("good", "* * *", "run", config)
    assert jobs.cron_add("good", "* * * * *", "run", config)["schedule"] == "* * * * *"
    assert "already exists" in jobs.cron_add("good", "* * * * *", "run", config)["error"]
    assert len(crontab.read_text().splitlines()) == 1


def test_scheduler_backend_reports_recorded_only_and_unavailable(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    crontab = tmp_path / "jarvis-crontab"
    monkeypatch.setattr(jobs, "_crontab_file", lambda *args: crontab)
    created = jobs.cron_add("local", "* * * * *", "run", config)
    assert created["scheduler"]["state"] == "recorded-only"
    assert created["scheduler"]["recorded"] is True
    assert created["scheduler"]["installed"] is False

    system_config = copy.deepcopy(config)
    system_config["jobs"]["backend"] = "system"
    monkeypatch.setattr(jobs.shutil, "which", lambda name: None)
    unavailable = jobs.cron_add("system", "* * * * *", "run", system_config)
    assert unavailable["scheduler"]["state"] == "unavailable"
    assert unavailable["scheduler"]["available"] is False
    assert "error" in unavailable


def test_job_run_status_is_persisted_and_redacted(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    crontab = tmp_path / "jarvis-crontab"
    monkeypatch.setattr(jobs, "_crontab_file", lambda *args: crontab)
    jobs.cron_add("status", "* * * * *", "run", config)
    jobs.set_job_status("status", "running", config=config, started="2026-08-01T10:00:00")
    running = jobs.get_job_status("status", config)
    assert running["status"] == "running"
    assert running["run_count"] == 1

    jobs.set_job_status("status", "succeeded", "api_key=hidden", config=config, started=running["last_run"])
    result = jobs.cron_status("status", config)
    assert result["status"]["status"] == "succeeded"
    assert "hidden" not in json.dumps(result)
    assert "last_status: succeeded" in (tmp_path / "mind" / "jobs" / "status.md").read_text()


def test_supervisor_can_write_crash_episode(tmp_path):
    config = _temp_config(tmp_path)
    log_crash(config, {"exit_code": 1, "stderr": "test"})
    assert list((tmp_path / "mind" / "crashes").glob("*.json"))


def test_guarded_registry_blocks_destructive_commands_and_paths(tmp_path):
    config = _temp_config(tmp_path)
    config["security"]["allowed_roots"] = [str(tmp_path)]
    registry = build_registry(tmp_path / "mind", config)
    blocked = registry.call("run_command", {"command": "rm -rf /tmp/example"})
    assert blocked["blocked"] is True
    outside = registry.call("write_file", {"file_path": "/tmp/example.txt", "content": "x"})
    assert outside["blocked"] is True
    inside = registry.call(
        "write_file", {"file_path": str(tmp_path / "ok.txt"), "content": "x"}
    )
    assert inside["bytes"] == 1
    registry.unregister("run_command")


def test_skill_parser_accepts_dream_cycle_format(tmp_path):
    path = tmp_path / "disk.md"
    path.write_text(
        "# Check disk\n\n## Steps\n1. Run df\n2. Report result\n\n"
        "## Tools needed\nrun_command, show_artifact\n"
    )
    parsed = _parse_skill(path)
    assert parsed["steps"] == ["Run df", "Report result"]
    assert parsed["tools_needed"] == ["run_command", "show_artifact"]


def test_approval_fallback_and_cancellable_command(tmp_path):
    config = _temp_config(tmp_path)
    config["security"]["allowed_roots"] = [str(tmp_path)]
    registry = build_registry(tmp_path / "mind", config)
    answers = []
    registry._jarvis_policy.set_approval_handler(type(
        "Approval", (), {"request": lambda self, *args: answers.append(args) or False}
    )())
    result = registry.call("run_command", {"command": "mv a b"})
    assert result["approval_required"] is True
    assert answers

    cancelled = threading.Event()
    timer = threading.Timer(0.1, cancelled.set)
    timer.start()
    result = _run_command("python -c \"import time; time.sleep(2)\"", 5, cancel_event=cancelled)
    timer.cancel()
    assert result["cancelled"] is True


def test_generated_code_validation_rejects_escape_hatches():
    assert "blocked import" in _validate_generated_code("import subprocess")
    assert "blocked call" in _validate_generated_code("eval('1 + 1')")
    assert _validate_generated_code("def register(registry):\n    return None") == ""


def test_secret_redaction():
    redacted = redact_text("api_key=sk_test_1234567890 password: hunter2 Bearer abc.def")
    assert "sk_test_1234567890" not in redacted
    assert "hunter2" not in redacted
    assert "abc.def" not in redacted


def test_llm_retries_transient_failures_without_network(tmp_path):
    config = _temp_config(tmp_path)
    config["llm"]["stream"] = False
    config["llm"]["retries"] = 2
    config["llm"]["retry_delay"] = 0
    llm = LLM(config)

    class FakeCompletions:
        attempts = 0

        def create(self, **kwargs):
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("temporary endpoint failure")
            message = type("Message", (), {"content": "ok", "tool_calls": None})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice]})()

    fake = FakeCompletions()
    llm.client = type("Client", (), {"chat": type("Chat", (), {"completions": fake})()})()
    assert llm.chat([{"role": "user", "content": "hi"}])["content"] == "ok"
    assert fake.attempts == 3


def test_llm_does_not_retry_after_partial_stream(tmp_path):
    config = _temp_config(tmp_path)
    config["llm"]["retries"] = 3
    config["llm"]["retry_delay"] = 0
    llm = LLM(config)

    class FakeCompletions:
        attempts = 0

        def create(self, **kwargs):
            self.attempts += 1
            chunk = type("Chunk", (), {
                "choices": [type("Choice", (), {
                    "delta": type("Delta", (), {"content": "partial", "tool_calls": []})()
                })()]
            })()
            def stream():
                yield chunk
                raise RuntimeError("stream ended unexpectedly")
            return stream()

    fake = FakeCompletions()
    llm.client = type("Client", (), {"chat": type("Chat", (), {"completions": fake})()})()
    try:
        llm.chat([{"role": "user", "content": "hi"}], on_text=lambda _: None)
    except LLMError:
        pass
    else:
        raise AssertionError("partial stream should fail")
    assert fake.attempts == 1


def test_registry_validates_required_tool_arguments(tmp_path):
    config = _temp_config(tmp_path)
    registry = ToolRegistry(config)
    result = registry.call("remember", {})
    assert result["validation_error"] is True
    assert "fact" in result["error"]


def test_agent_writes_redacted_audit_record(tmp_path):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)

    class FakeLLM:
        def chat(self, messages, tools=None, on_text=None):
            if on_text:
                on_text("done")
            return {"role": "assistant", "content": "done"}

    jarvis.llm = FakeLLM()
    jarvis.ask("my api_key=secret_value")
    audit_files = list((Path(config["paths"]["mind"]) / "audit").glob("*.jsonl"))
    content = audit_files[0].read_text()
    assert "secret_value" not in content
    assert "turn_finished" in content


def test_agent_retries_last_user_turn_as_a_new_turn(tmp_path):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)

    class FakeLLM:
        calls = 0

        def chat(self, messages, tools=None, on_text=None):
            self.calls += 1
            reply = f"response {self.calls}"
            if on_text:
                on_text(reply)
            return {"role": "assistant", "content": reply}

    fake = FakeLLM()
    jarvis.llm = fake
    assert jarvis.ask("check the deployment") == "response 1"
    assert jarvis.last_user_message() == "check the deployment"
    assert jarvis.retry_last_turn() == "response 2"
    assert fake.calls == 2
    assert [message["role"] for message in jarvis.history].count("user") == 2
    jarvis.registry.stop_watcher()


def test_ui_memory_review_surface_can_approve_pending_memory(tmp_path):
    config = _temp_config(tmp_path)
    FactStore(config["paths"]["mind"]).remember(
        "The user prefers a compact review panel.",
        name="pending-ui-preference",
        source="consolidation",
        review_status="pending",
    )
    jarvis = Jarvis(config)

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#welcome")
            assert app.query_one("#send").label.plain == "Send  ↵"
            assert app.query_one("#input").placeholder == "Type a message to Jarvis…"
            panel = app.query_one("#memory-review-list")
            assert any(option.id == "pending-ui-preference" for option in panel.options)
            app._selected_memory = "pending-ui-preference"
            app.on_button_pressed(SimpleNamespace(
                button=SimpleNamespace(id="memory-approve")
            ))
            await pilot.pause()
            assert FactStore(config["paths"]["mind"]).review_memories()["count"] == 0

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_slash_command_is_local_and_does_not_start_a_turn(tmp_path):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#input").value = "/model ui-model"
            app._submit_input()
            await pilot.pause()
            assert jarvis.config["llm"]["model"] == "ui-model"
            assert jarvis.history == []
            assert any("Model changed to: ui-model" in card.render().plain for card in app.query(".system"))

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_slash_palette_can_filter_and_insert_commands(tmp_path):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/prov"
            await pilot.pause()
            palette = app.query_one("#command-palette")
            assert palette.has_class("visible")
            assert [option.id for option in palette.options] == ["0"]
            await pilot.press("tab")
            assert box.value == "/provider "
            assert palette.has_class("visible")
            assert [option.id for option in palette.options] == ["0", "1", "2", "3", "4"]
            box.value = "/settings "
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SlashFormScreen)

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_sets_endpoint_key_and_model(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    monkeypatch.setattr("jarvis.ui._test_connection", _fake_test_connection)
    jarvis.llm.probe = lambda timeout=5.0: (
        True, "Connected — 2 model(s) listed", ["a", "b"], 200
    )

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider use"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            status = app.screen.query_one("#field-status", Static).render().plain
            assert "Connected — 2 model(s) listed" in status

            provider_panel = app.screen.query_one("#field-provider", OptionList)
            provider_panel.highlighted = provider_panel.option_count - 1  # custom
            app.screen.query_one("#field-url", Input).value = "http://example.test:6446"
            app.screen.query_one("#field-key", Input).value = (
                "oc-1b50289171f4709611efefc0613603ae3ecc6355"
            )
            model_panel = app.screen.query_one("#field-model", OptionList)
            model_panel.highlighted = 2  # "b"
            app.screen.on_button_pressed(SimpleNamespace(
                button=SimpleNamespace(id="form-submit")
            ))
            await pilot.pause()
            await pilot.pause()
            assert jarvis.config["llm"]["provider"] == "custom"
            assert jarvis.config["llm"]["base_url"] == "http://example.test:6446"
            assert jarvis.config["llm"]["api_key"] == (
                "oc-1b50289171f4709611efefc0613603ae3ecc6355"
            )
            assert jarvis.config["llm"]["model"] == "b"
            assert any(
                "Provider changed to custom" in card.render().plain
                for card in app.query(".system")
            )

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_preset_applies_provider(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    monkeypatch.setattr("jarvis.ui._test_connection", _fake_test_connection)
    jarvis.llm.probe = lambda timeout=5.0: (
        True, "Connected — 2 model(s) listed", ["a", "b"], 200
    )

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            provider_panel = app.screen.query_one("#field-provider", OptionList)
            provider_panel.highlighted = 1  # ollama
            app.screen.on_button_pressed(SimpleNamespace(
                button=SimpleNamespace(id="form-submit")
            ))
            await pilot.pause()
            await pilot.pause()
            assert jarvis.config["llm"]["provider"] == "ollama"
            assert jarvis.config["llm"]["base_url"] == "http://localhost:11434/v1"

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_auto_refreshes_models_on_url_change(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)

    def fake_connection(jarvis, base_url=None, api_key_env=None, api_key=None, timeout=5.0, check_model=True):
        url = base_url or jarvis.config["llm"].get("base_url") or "http://example.test/v1"
        ids = ["c", "d", "e"] if "beta" in url else ["a", "b"]
        return True, "Connected", ids, url

    monkeypatch.setattr("jarvis.ui._test_connection", fake_connection)
    jarvis.llm.probe = lambda timeout=5.0: (True, "Connected", ["a", "b"], 200)

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            app.screen._test_debounce = 0.05
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            models = app.screen.query_one("#field-model", OptionList)
            assert [option.id for option in models.options] == [
                "openai/gpt-oss-20b", "a", "b",
            ]

            url_input = app.screen.query_one("#field-url", Input)
            url_input.value = "http://alpha.test/v1"
            await asyncio.sleep(0.2)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            assert [option.id for option in models.options] == [
                "openai/gpt-oss-20b", "a", "b",
            ]

            url_input.value = "http://beta.test/v1"
            await asyncio.sleep(0.2)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            assert [option.id for option in models.options] == [
                "openai/gpt-oss-20b", "c", "d", "e",
            ]

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_small_terminal_stays_unscrolled(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    monkeypatch.setattr("jarvis.ui._test_connection", _fake_test_connection)
    jarvis.llm.probe = lambda timeout=5.0: (
        True, "Connected — 2 model(s) listed", ["a", "b"], 200
    )

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            form_box = app.screen.query_one("#form-box")
            assert form_box.scroll_y == 0, (
                f"form-box auto-scrolled to y={form_box.scroll_y} on mount"
            )
            assert app.screen.query_one("#form-title").region.bottom <= (
                form_box.region.bottom
            )
            assert form_box.region.bottom <= app.screen.region.bottom
            submit = app.screen.query_one("#form-submit")
            if submit.region.bottom > form_box.region.bottom:
                form_box.scroll_end()
                await pilot.pause()
            assert app.screen.query_one("#form-submit").region.bottom <= (
                form_box.region.bottom
            ), "Submit not reachable after scrolling the form"
            app.screen.on_button_pressed(SimpleNamespace(
                button=SimpleNamespace(id="form-cancel")
            ))
            await pilot.pause()

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_small_terminal_keeps_focus_scroll_off(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    monkeypatch.setattr("jarvis.ui._test_connection", _fake_test_connection)
    jarvis.llm.probe = lambda timeout=5.0: (
        True, "Connected — 2 model(s) listed", ["a", "b"], 200
    )

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            form_box = app.screen.query_one("#form-box")
            assert form_box.scroll_y == 0
            assert form_box.region.bottom <= app.screen.region.bottom
            assert app.screen.query_one("#form-title").region.bottom <= (
                form_box.region.bottom
            )
            app.screen.query_one("#field-url", Input).focus()
            await pilot.pause()
            assert form_box.scroll_y == 0, (
                "focusing an already-visible input scrolled the form box"
            )
            app.screen.on_button_pressed(SimpleNamespace(
                button=SimpleNamespace(id="form-cancel")
            ))
            await pilot.pause()

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_keeps_models_when_retest_fails(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)

    def fake_connection(jarvis, base_url=None, api_key_env=None, api_key=None, timeout=5.0, check_model=True):
        url = base_url or jarvis.config["llm"].get("base_url") or "http://example.test/v1"
        if "bad" in url:
            return False, "HTTP 401: Invalid API key", [], 401
        return True, "Connected", ["a", "b"], url

    monkeypatch.setattr("jarvis.ui._test_connection", fake_connection)
    jarvis.llm.probe = lambda timeout=5.0: (True, "Connected", ["a", "b"], 200)

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            app.screen._test_debounce = 0.05
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            models = app.screen.query_one("#field-model", OptionList)
            assert [option.id for option in models.options] == [
                "openai/gpt-oss-20b", "a", "b",
            ]

            url_input = app.screen.query_one("#field-url", Input)
            url_input.value = "http://bad.test/v1"
            await asyncio.sleep(0.2)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            status = app.screen.query_one("#field-status", Static).render().plain
            assert "HTTP 401" in status
            assert [option.id for option in models.options] == [
                "openai/gpt-oss-20b", "a", "b",
            ]

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    monkeypatch.setattr("jarvis.ui._test_connection", _fake_test_connection)

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/model"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, SlashFormScreen)
            field = app.screen.query_one("#field-model", Input)
            field.value = "my-model"
            await pilot.press("enter")
            await pilot.pause()
            assert jarvis.config["llm"]["model"] == "my-model"
            assert app.query_one("#input").value == ""

            box.value = "/provider use"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert jarvis.config["llm"]["provider"] == "custom"
            assert not isinstance(app.screen, ProviderScreen)

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_autofixes_missing_v1_in_url_field(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)

    def fake_connection(jarvis, base_url=None, api_key_env=None, api_key=None, timeout=5.0, check_model=True):
        url = base_url or jarvis.config["llm"].get("base_url") or "http://example.test/v1"
        if not url.rstrip("/").endswith("/v1"):
            return True, "Connected (appended /v1)", ["a", "b"], url.rstrip("/") + "/v1"
        return True, "Connected", ["a", "b"], url

    monkeypatch.setattr("jarvis.ui._test_connection", fake_connection)
    jarvis.llm.probe = lambda timeout=5.0: (True, "Connected", ["a", "b"], 200)

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            app.screen._test_debounce = 0.05
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            url_field = app.screen.query_one("#field-url", Input)
            url_field.value = "http://host.test:1234"
            await asyncio.sleep(0.2)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            assert url_field.value == "http://host.test:1234/v1", (
                "URL field was not updated with the working /v1 URL"
            )
            status = app.screen.query_one("#field-status", Static).render().plain
            assert "OK" in status
            app.screen.on_button_pressed(SimpleNamespace(
                button=SimpleNamespace(id="form-cancel")
            ))
            await pilot.pause()

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_test_skips_configured_model_check(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    seen = {}

    def fake_connection(jarvis, base_url=None, api_key_env=None, api_key=None, timeout=5.0, check_model=True):
        seen["check_model"] = check_model
        url = base_url or jarvis.config["llm"].get("base_url") or "http://example.test/v1"
        return True, "Connected — 2 model(s) listed", ["a", "b"], url

    monkeypatch.setattr("jarvis.ui._test_connection", fake_connection)
    jarvis.llm.probe = lambda timeout=5.0: (True, "Connected", ["a", "b"], 200)

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            assert seen.get("check_model") is False, (
                "provider form test must not verify the saved config model"
            )
            app.screen.on_button_pressed(SimpleNamespace(
                button=SimpleNamespace(id="form-cancel")
            ))
            await pilot.pause()

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_ui_provider_manager_prefers_remembered_model_for_provider(tmp_path, monkeypatch):
    config = _temp_config(tmp_path)
    jarvis = Jarvis(config)
    from jarvis.config import remember_model

    remember_model(config, "ollama", "deepseek-r1")
    monkeypatch.setattr("jarvis.ui._test_connection", _fake_test_connection)
    jarvis.llm.probe = lambda timeout=5.0: (
        True, "Connected — 2 model(s) listed", ["a", "b"], 200
    )

    async def exercise():
        app = JarvisUI(jarvis)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#input")
            box.value = "/provider"
            app._submit_input()
            await pilot.pause()
            assert isinstance(app.screen, ProviderScreen)
            app.screen._test_debounce = 0.05
            provider_panel = app.screen.query_one("#field-provider", OptionList)
            provider_panel.highlighted = 1  # ollama
            app.screen.on_option_list_option_selected(SimpleNamespace(
                option_list=SimpleNamespace(id="field-provider"),
                option=SimpleNamespace(id="ollama"),
            ))
            await app.screen._last_test_worker.wait()
            await pilot.pause()
            models = app.screen.query_one("#field-model", OptionList)
            assert [option.id for option in models.options] == [
                "deepseek-r1", "a", "b",
            ]
            assert models.highlighted == 0
            app.screen.on_button_pressed(SimpleNamespace(
                button=SimpleNamespace(id="form-cancel")
            ))
            await pilot.pause()

    try:
        asyncio.run(exercise())
    finally:
        jarvis.registry.stop_watcher()


def test_waveform_changes_with_tts_amplitude():
    waveform = Waveform()
    waveform.set_state("Speaking")
    waveform.set_amplitude(0.03)
    for _ in range(4):
        waveform.tick()
    quiet = waveform.render().plain

    for _ in range(12):
        waveform.set_amplitude(0.95)
        waveform.tick()
    loud = waveform.render().plain
    assert quiet != loud
    assert "Speaking" in loud
