"""Voice engines and model catalog. Hermetic: no network, no audio, no real pip."""

import copy
import io
import subprocess
import sys
import wave
from types import SimpleNamespace

import pytest

from jarvis import models_manager as mm
from jarvis import voice as voice_mod
from jarvis.config import DEFAULTS


def make_config(tmp_path, **voice):
    config = copy.deepcopy(DEFAULTS)
    config["paths"]["models"] = str(tmp_path / "models")
    config["paths"]["mind"] = str(tmp_path / "mind")
    config["voice"].update(voice)
    return config


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_transport(monkeypatch, payload=b"x" * 5000, fail_on=None):
    urls = []

    def opener(url):
        urls.append(url)
        if fail_on and fail_on in url:
            raise OSError("boom")
        return FakeResponse(payload)

    monkeypatch.setattr(mm, "_open_url", opener)
    return urls


def make_installed(tmp_path, item_id):
    for _url, dest, _size in mm._spec(item_id)["files"]:
        path = tmp_path / "models" / dest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")


# ---------- catalog ----------

def test_catalog_shape_and_ids(tmp_path):
    items = mm.catalog(make_config(tmp_path))
    ids = {item["id"] for item in items}
    assert {"piper:en_US-lessac-medium", "piper:en_US-amy-medium", "piper:en_GB-alan-medium",
            "piper:en_US-ryan-high", "kokoro:v1.0", "whisper:tiny", "whisper:base",
            "whisper:small", "embeddings:potion-base-8M"} <= ids
    for item in items:
        assert set(item) == {"id", "kind", "engine", "label", "size_mb", "installed",
                             "package", "package_installed", "default"}
        assert item["kind"] in ("tts", "stt", "embeddings")
        assert item["size_mb"] >= 1
    by_id = {item["id"]: item for item in items}
    assert by_id["piper:en_US-lessac-medium"]["default"] is True
    assert by_id["kokoro:v1.0"]["package"] == "kokoro-onnx"
    assert by_id["kokoro:v1.0"]["size_mb"] == 121


def test_is_installed_reflects_files(tmp_path):
    config = make_config(tmp_path)
    assert not mm.is_installed("piper:en_US-lessac-medium", config)
    make_installed(tmp_path, "piper:en_US-lessac-medium")
    assert mm.is_installed("piper:en_US-lessac-medium", config)
    make_installed(tmp_path, "whisper:tiny")
    assert mm.is_installed("whisper:tiny", config)
    with pytest.raises(KeyError):
        mm.is_installed("nope:1", config)


def test_kokoro_urls_use_int8_release_assets():
    urls = [u for u, _d, _s in mm._spec("kokoro:v1.0")["files"]]
    assert urls[0].endswith("/model-files-v1.0/kokoro-v1.0.int8.onnx")
    assert urls[1].endswith("/model-files-v1.0/voices-v1.0.bin")


# ---------- download ----------

def test_download_streams_atomically_with_progress(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    urls = fake_transport(monkeypatch)
    events = []
    mm.download("piper:en_US-lessac-medium", events.append, config)
    assert len(urls) == 2 and urls[0].endswith("en_US-lessac-medium.onnx")
    folder = tmp_path / "models" / "piper" / "en_US-lessac-medium"
    assert (folder / "en_US-lessac-medium.onnx").read_bytes() == b"x" * 5000
    assert not list(folder.glob("*.part"))
    assert events[0]["status"] == "downloading" and events[0]["progress"] == 0.0
    assert events[-1] == {"item_id": "piper:en_US-lessac-medium", "status": "done",
                          "progress": 1.0, "message": events[-1]["message"]}
    assert all(e["item_id"] == "piper:en_US-lessac-medium" for e in events)
    assert mm.is_installed("piper:en_US-lessac-medium", config)


def test_download_failure_reports_error_and_leaves_no_partial(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    fake_transport(monkeypatch, fail_on=".onnx.json")
    events = []
    with pytest.raises(OSError):
        mm.download("piper:en_US-lessac-medium", events.append, config)
    assert events[-1]["status"] == "error" and "boom" in events[-1]["message"]
    assert not list((tmp_path / "models").rglob("*.part"))
    assert not mm.is_installed("piper:en_US-lessac-medium", config)


def test_download_installs_missing_package_first(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    fake_transport(monkeypatch)
    state = {"installed": False}
    monkeypatch.setattr(mm, "_package_installed", lambda group: state["installed"] or group != "kokoro")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        state["installed"] = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    events = []
    mm.download("kokoro:v1.0", events.append, config)
    assert calls[0][:5] == [sys.executable, "-m", "pip", "install", "kokoro-onnx"]
    assert "--ignore-requires-python" in calls[0]
    assert [e["status"] for e in events][0] == "installing"
    assert events[-1]["status"] == "done"


def test_pip_failure_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "_package_installed", lambda group: False)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="ERROR: no matching distribution"))
    events = []
    with pytest.raises(RuntimeError, match="pip install"):
        mm.download("kokoro:v1.0", events.append, make_config(tmp_path))
    assert events[-1]["status"] == "error"


def test_embeddings_download_uses_hf_cache(tmp_path, monkeypatch):
    fetched = []
    monkeypatch.setattr(mm, "_hf_download", lambda repo, name: fetched.append((repo, name)))
    monkeypatch.setattr(mm, "_embeddings_cached", lambda: False)
    monkeypatch.setattr(mm, "_package_installed", lambda group: True)
    events = []
    mm.download("embeddings:potion-base-8M", events.append, make_config(tmp_path))
    assert ("minishlab/potion-base-8M", "model.safetensors") in fetched
    assert events[-1]["status"] == "done"


# ---------- engines ----------

class FakePiperModel:
    config = SimpleNamespace(sample_rate=22050)

    def synthesize(self, text):
        yield SimpleNamespace(audio_int16_bytes=b"\x01\x00" * 100, sample_rate=22050)


def piper_engine(tmp_path, monkeypatch):
    config = make_config(tmp_path, model="en_US-lessac-medium")
    make_installed(tmp_path, "piper:en_US-lessac-medium")
    engine = voice_mod.make_engine(config)
    engine._voice = FakePiperModel()
    engine._loaded_name = "en_US-lessac-medium"
    monkeypatch.setattr(voice_mod.PiperVoice, "_package_name", lambda self: "sys")  # importable
    return engine


def test_piper_synthesize_returns_wav(tmp_path, monkeypatch):
    engine = piper_engine(tmp_path, monkeypatch)
    assert engine.ready() == (True, None)
    data = engine.synthesize("Hello there. Second sentence! `code` ```skip```")
    with wave.open(io.BytesIO(data)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 22050)
        assert wav.getnframes() == 300  # three sentences, 100 frames each


def test_ready_reports_missing_item(tmp_path):
    engine = voice_mod.make_engine(make_config(tmp_path))
    ok, missing = engine.ready()
    assert not ok and missing == "piper:en_US-lessac-medium"
    with pytest.raises(RuntimeError):
        engine.synthesize("hi")


def test_kokoro_engine_with_fake_runtime(tmp_path, monkeypatch):
    import numpy as np
    config = make_config(tmp_path, engine="kokoro", kokoro_voice="af_sky")
    make_installed(tmp_path, "kokoro:v1.0")
    engine = voice_mod.make_engine(config)
    assert isinstance(engine, voice_mod.KokoroVoice)
    monkeypatch.setattr(voice_mod.KokoroVoice, "_package_name", lambda self: "sys")
    seen = {}

    class FakeKokoro:
        def create(self, text, voice, speed, lang):
            seen["voice"] = voice
            return np.full(240, 0.5, dtype=np.float32), 24000

    engine._kokoro = FakeKokoro()
    data = engine.synthesize("Hi.")
    assert seen["voice"] == "af_sky"
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getframerate() == 24000 and wav.getnframes() == 240


def test_get_voice_null_when_disabled_but_make_engine_always_builds(tmp_path):
    config = make_config(tmp_path, enabled=False)
    assert isinstance(voice_mod.get_voice(config), voice_mod.NullVoice)
    assert isinstance(voice_mod.make_engine(config), voice_mod.PiperVoice)
    config["voice"]["enabled"] = True
    assert isinstance(voice_mod.get_voice(config), voice_mod.PiperVoice)


def test_speak_plays_through_fake_device_and_emits_amplitude(tmp_path, monkeypatch):
    engine = piper_engine(tmp_path, monkeypatch)
    played, levels = [], []
    monkeypatch.setattr(engine, "_play", lambda pcm, rate: played.append((len(pcm), rate)))
    engine.set_amplitude_callback(levels.append)
    engine.speak("One. Two.")
    assert played == [(200, 22050), (200, 22050)]
    assert levels[-1] == 0.0 and not engine.is_speaking


# ---------- /voice command ----------

class FakeJarvis:
    def __init__(self, config):
        self.config = config
        self.refreshed = 0

    def refresh_voice(self):
        self.refreshed += 1


def test_voice_command_status_on_off_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_mod, "_persist", lambda jarvis, *keys: None)
    jarvis = FakeJarvis(make_config(tmp_path))
    assert "Auto-speak: off" in voice_mod.handle_voice_command(jarvis, "status")
    out = voice_mod.handle_voice_command(jarvis, "on")
    assert jarvis.config["voice"]["enabled"] is True and "download piper:en_US-lessac-medium" in out
    voice_mod.handle_voice_command(jarvis, "off")
    assert jarvis.config["voice"]["enabled"] is False
    voice_mod.handle_voice_command(jarvis, "engine kokoro")
    assert jarvis.config["voice"]["engine"] == "kokoro"
    assert "Usage" in voice_mod.handle_voice_command(jarvis, "engine nope")
    assert jarvis.refreshed == 3
    assert "kokoro:v1.0" in voice_mod.handle_voice_command(jarvis, "models")


def test_voice_command_download_prints_progress(tmp_path, monkeypatch, capsys):
    jarvis = FakeJarvis(make_config(tmp_path))
    fake_transport(monkeypatch)
    monkeypatch.setattr(mm, "_package_installed", lambda group: True)
    out = voice_mod.handle_voice_command(jarvis, "download whisper:tiny")
    assert "ready" in out and jarvis.refreshed == 1
    assert "[voice] whisper:tiny" in capsys.readouterr().out
    assert "Unknown" in voice_mod.handle_voice_command(jarvis, "download bogus")
