"""Suite-wide hermeticity: never let a test pick up the locally cached embedding model."""

import pytest


@pytest.fixture(autouse=True)
def _no_embedding_model(monkeypatch):
    monkeypatch.setattr("jarvis.tool_exposure.embedding_model_cached", lambda: False)


@pytest.fixture(autouse=True)
def _tmp_keystore(monkeypatch, tmp_path):
    """Never read or write the real saved-keys file."""
    monkeypatch.setenv("JARVIS_KEYS_FILE", str(tmp_path / "keys" / "keys.json"))
