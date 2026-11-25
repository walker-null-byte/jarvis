"""Suite-wide hermeticity: never let a test pick up the locally cached embedding model."""

import pytest


@pytest.fixture(autouse=True)
def _no_embedding_model(monkeypatch):
    monkeypatch.setattr("jarvis.tool_exposure.embedding_model_cached", lambda: False)
