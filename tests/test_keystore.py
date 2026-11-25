"""Saved API keys: file rules, key resolution order, redaction, and the web endpoints."""

import stat

import pytest

pytest.importorskip("fastapi")

from jarvis import keystore
from jarvis.providers.openai_chat import resolve_key
from jarvis.redaction import redact_text
from tests.test_webui import BASE, TOKEN, Env, env  # noqa: F401  (fixture reuse)


def test_set_get_delete_names_hint():
    assert keystore.get("GEMINI_API_KEY") is None and keystore.names() == []
    keystore.set("GEMINI_API_KEY", "  abcd1234wxyz ")
    keystore.set("agent:lead:TOKEN", "t0k3n-value")
    assert keystore.get("GEMINI_API_KEY") == "abcd1234wxyz"
    assert keystore.names() == ["GEMINI_API_KEY", "agent:lead:TOKEN"]
    assert keystore.hint("GEMINI_API_KEY") == "••••wxyz"
    assert keystore.hint("MISSING") == ""
    assert keystore.delete("GEMINI_API_KEY") is True and keystore.delete("GEMINI_API_KEY") is False
    keystore.set("agent:lead:TOKEN", "")  # empty value deletes
    assert keystore.names() == []


def test_file_and_dir_permissions():
    keystore.set("OPENAI_API_KEY", "sk-secret-0000")
    path = keystore.keys_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(path.parent.glob("*.tmp"))


def test_corrupt_file_reads_as_empty():
    keystore.keys_path().parent.mkdir(parents=True)
    keystore.keys_path().write_text("{not json")
    assert keystore.names() == []
    keystore.set("A_KEY", "value-123456")
    assert keystore.get("A_KEY") == "value-123456"


def test_default_path_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_KEYS_FILE")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert keystore.keys_path() == tmp_path / "xdg" / "jarvis" / "keys.json"


def test_resolve_key_order(monkeypatch):
    llm = {"api_key": None, "api_key_env": "GEMINI_API_KEY"}
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert resolve_key(llm) == "not-needed"
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert resolve_key(llm) == "from-env"
    keystore.set("GEMINI_API_KEY", "from-store")
    assert resolve_key(llm) == "from-store"          # saved beats env
    assert resolve_key({**llm, "api_key": "literal"}) == "literal"  # literal beats saved
    assert resolve_key({"api_key": None, "api_key_env": None}) == "not-needed"


def test_saved_values_are_redacted():
    keystore.set("X_KEY", "supersecretvalue99")
    assert "supersecretvalue99" not in redact_text("the key is supersecretvalue99 ok")
    keystore.delete("X_KEY")
    assert "supersecretvalue99" in redact_text("supersecretvalue99")


def test_key_endpoints_never_return_values(env):
    client = env.client
    assert client.get("/api/keys").json() == {"keys": []}
    out = client.put("/api/keys/GROQ_API_KEY", json={"value": "gsk-very-secret-4242"})
    assert out.status_code == 200 and out.json()["hint"] == "••••4242"
    listing = client.get("/api/keys")
    assert listing.json() == {"keys": [{"name": "GROQ_API_KEY", "hint": "••••4242"}]}
    assert "gsk-very-secret" not in listing.text
    assert client.put("/api/keys/bad name!", json={"value": "x"}).status_code == 400
    assert client.put("/api/keys/GROQ_API_KEY", json={"value": " "}).status_code == 400
    providers = client.get("/api/providers")
    by_id = {p["id"]: p for p in providers.json()["providers"]}
    assert by_id["groq"]["saved"] is True and by_id["groq"]["saved_hint"] == "••••4242"
    assert by_id["openai"]["saved"] is False
    assert "gsk-very-secret" not in providers.text
    assert client.delete("/api/keys/GROQ_API_KEY").status_code == 200
    assert client.delete("/api/keys/GROQ_API_KEY").status_code == 404
    assert keystore.names() == []


def test_apply_with_remember_saves_key_and_uses_it(env, monkeypatch):
    import jarvis.commands as commands
    monkeypatch.setattr(commands, "_test_connection", lambda *a, **k: (True, "ok", [], "x"))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    out = env.client.post("/api/provider", json={
        "provider": "groq", "api_key_env": "gsk-remember-me-9876", "remember": True})
    assert out.status_code == 200 and "gsk-remember-me" not in out.text
    assert keystore.get("GROQ_API_KEY") == "gsk-remember-me-9876"
    llm = env.jarvis.config["llm"]
    assert llm["provider"] == "groq" and not llm.get("api_key")
    assert env.jarvis.llm.api_key == "gsk-remember-me-9876"
    # Without the flag the key stays session-only.
    env.client.post("/api/provider", json={"provider": "mistral", "api_key_env": "mis-session-5555"})
    assert keystore.get("MISTRAL_API_KEY") is None
    assert env.jarvis.config["llm"]["api_key"] == "mis-session-5555"


def test_forget_switches_client_back_to_no_key(env, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    env.jarvis.config["llm"].update(provider="groq", api_key_env="GROQ_API_KEY", api_key=None)
    env.client.put("/api/keys/GROQ_API_KEY", json={"value": "gsk-abc-123456"})
    assert env.jarvis.llm.api_key == "gsk-abc-123456"
    env.client.delete("/api/keys/GROQ_API_KEY")
    assert env.jarvis.llm.api_key == "not-needed"
