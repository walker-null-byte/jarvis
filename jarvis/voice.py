"""Local text-to-speech: Piper (default) and Kokoro (kokoro-onnx).

Engines turn text into WAV bytes (`synthesize`, used by the web UI, where the
browser plays the audio) or play it through the local sound card (`speak`,
used by the CLI/TUI). Models come from `jarvis.models_manager`; they are never
downloaded implicitly.
"""

import io
import os
import re
import threading
import wave

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

from jarvis import models_manager
from jarvis.models_manager import ModelManager


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap 16-bit mono PCM in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class Voice:
    """Base TTS engine. Subclasses implement _load / _item_id / _pcm."""

    engine = "base"

    def __init__(self, config):
        self.config = config
        self.models = ModelManager(models_manager.models_dir(config))
        self._amplitude_cb = None
        self._speaking = False
        self._target_sr = 44100  # playback rate (pulseaudio/pipewire default)
        self._speak_thread = None
        self._audio_device = config.get("voice", {}).get("audio_device") or None
        self._lock = threading.Lock()

    # ----- engine hooks -----

    def _item_id(self) -> str:
        """Catalog id of the model files this engine needs."""
        raise NotImplementedError

    def _files_present(self) -> bool:
        raise NotImplementedError

    def _package_name(self) -> str:
        raise NotImplementedError

    def _pcm(self, sentence: str):
        """Synthesize one sentence -> (int16 PCM bytes, sample_rate)."""
        raise NotImplementedError

    # ----- public API -----

    def ready(self):
        """(True, None) when package + model files are usable, else (False, item_id)."""
        import importlib.util
        if importlib.util.find_spec(self._package_name()) is None:
            return False, self._item_id()
        if not self._files_present():
            return False, self._item_id()
        return True, None

    def synthesize(self, text: str) -> bytes:
        """Return a complete WAV file (16-bit PCM mono) of speech_text(text)."""
        text = self.speech_text(text)
        if not text:
            raise ValueError("nothing to speak")
        ok, missing = self.ready()
        if not ok:
            raise RuntimeError(f"voice model not ready: {missing}")
        pieces, rate = [], None
        with self._lock:
            for sentence in _sentences(text) or [text]:
                pcm, rate = self._pcm(sentence)
                pieces.append(pcm)
        return _wav_bytes(b"".join(pieces), rate or 22050)

    def speak(self, text: str):
        """Synthesize and play locally, sentence by sentence (blocking)."""
        text = self.speech_text(text)
        if not text or not self.ready()[0]:
            return
        self._speaking = True
        try:
            for sentence in _sentences(text) or [text]:
                if not self._speaking:
                    break
                with self._lock:
                    pcm, rate = self._pcm(sentence)
                self._play(pcm, rate)
        except Exception as error:
            print(f"[voice] {self.engine} failed: {error}")
        finally:
            self._speaking = False
            self._emit_amplitude(0.0)

    def stop(self):
        self._speaking = False
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def wait(self):
        """Wait for current speech to finish."""
        if self._speak_thread and self._speak_thread.is_alive():
            self._speak_thread.join()

    @property
    def is_speaking(self):
        return self._speaking

    def set_amplitude_callback(self, cb):
        self._amplitude_cb = cb

    # ----- helpers -----

    def _emit_amplitude(self, amp):
        if self._amplitude_cb:
            self._amplitude_cb(amp)

    @staticmethod
    def _measure_amplitude(audio):
        """Return a stable 0..1 level from an int16 audio frame."""
        if np is None:
            return 0.0
        try:
            values = np.asarray(audio, dtype=np.float32)
        except Exception:
            return 0.0
        if values.size == 0:
            return 0.0
        values = values / 32768.0
        peak = float(np.max(np.abs(values)))
        rms = float(np.sqrt(np.mean(values * values)))
        return max(0.0, min(1.0, rms * 4.0 + peak * 0.15))

    def _resample(self, audio, src_sr: int, dst_sr: int):
        """Simple linear interpolation resampling."""
        if np is None or src_sr == dst_sr:
            return audio
        new_len = int(len(audio) * dst_sr / src_sr)
        indices = np.linspace(0, len(audio) - 1, new_len)
        return np.interp(indices, np.arange(len(audio)), audio).astype(np.int16)

    def _play(self, pcm: bytes, rate: int):
        """Play PCM through sounddevice in 40 ms frames (feeds the amplitude meter)."""
        try:
            import sounddevice as sd
            audio = self._resample(np.frombuffer(pcm, dtype=np.int16), rate, self._target_sr)
            kwargs = {"samplerate": self._target_sr, "channels": 1, "dtype": "int16"}
            if self._audio_device:
                kwargs["device"] = self._audio_device
            frame = max(1, int(self._target_sr * 0.04))
            with sd.OutputStream(**kwargs) as stream:
                for start in range(0, len(audio), frame):
                    if not self._speaking:
                        break
                    part = audio[start:start + frame]
                    self._emit_amplitude(self._measure_amplitude(part))
                    stream.write(part)
        except Exception:
            self._play_with_aplay(pcm, rate)

    def _play_with_aplay(self, pcm: bytes, rate: int):
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = handle.name
        try:
            with open(path, "wb") as out:
                out.write(_wav_bytes(pcm, rate))
            self._emit_amplitude(self._measure_amplitude(np.frombuffer(pcm, dtype=np.int16)))
            subprocess.run(["aplay", "-q", path], capture_output=True, check=False)
        finally:
            os.unlink(path)

    @staticmethod
    def speech_text(text):
        """Return only the text intended for speech, excluding code/markup."""
        tagged = re.findall(r"<say>(.*?)</say>", text, flags=re.IGNORECASE | re.DOTALL)
        if tagged:
            return " ".join(tagged).strip()
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        return text.strip()


class NullVoice(Voice):
    """No-op voice used when auto-speak is off."""

    engine = "none"

    def speak(self, text: str):
        self._emit_amplitude(0.0)

    def ready(self):
        return False, None


class PiperVoice(Voice):
    """Piper TTS: fast, lightweight, good quality."""

    engine = "piper"

    def __init__(self, config):
        super().__init__(config)
        self._voice = None
        self._loaded_name = None

    def _name(self):
        return self.config.get("voice", {}).get("model") or "en_US-lessac-medium"

    def _item_id(self):
        return f"piper:{self._name()}"

    def _package_name(self):
        return "piper"

    def _files_present(self):
        return self.models.ensure_piper(self._name())[0] is not None

    def _load(self):
        name = self._name()
        if self._voice is None or self._loaded_name != name:
            onnx, meta = self.models.ensure_piper(name)
            if onnx is None:
                raise RuntimeError(f"Piper voice {name} is not downloaded")
            from piper import PiperVoice as PiperModel
            self._voice = PiperModel.load(str(onnx), config_path=str(meta))
            self._loaded_name = name
        return self._voice

    def _pcm(self, sentence):
        model = self._load()
        chunks = list(model.synthesize(sentence))
        pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        rate = chunks[0].sample_rate if chunks else model.config.sample_rate
        return pcm, rate


class KokoroVoice(Voice):
    """Kokoro TTS through the small kokoro-onnx runtime (no torch)."""

    engine = "kokoro"

    def __init__(self, config):
        super().__init__(config)
        self._kokoro = None

    def _item_id(self):
        return "kokoro:v1.0"

    def _package_name(self):
        return "kokoro_onnx"

    def _files_present(self):
        return self.models.ensure_kokoro()[0] is not None

    def _load(self):
        if self._kokoro is None:
            model, voices = self.models.ensure_kokoro()
            if model is None:
                raise RuntimeError("Kokoro model files are not downloaded")
            from kokoro_onnx import Kokoro
            self._kokoro = Kokoro(str(model), str(voices))
        return self._kokoro

    def _pcm(self, sentence):
        kokoro = self._load()
        voice = self.config.get("voice", {}).get("kokoro_voice") or "af_sky"
        samples, rate = kokoro.create(sentence, voice=voice, speed=1.0, lang="en-us")
        audio = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype(np.int16)
        return audio.tobytes(), int(rate)


def make_engine(config):
    """Build the configured engine regardless of voice.enabled."""
    if config.get("voice", {}).get("engine") == "kokoro":
        return KokoroVoice(config)
    return PiperVoice(config)


def get_voice(config):
    """Engine used for auto-speak; NullVoice when voice.enabled is False."""
    if not config.get("voice", {}).get("enabled", False):
        return NullVoice(config)
    return make_engine(config)


# ---------- /voice slash command ----------

_VOICE_HELP = ("Usage: /voice status | on | off | engine <piper|kokoro> | models | "
               "download <item_id>")


def _persist(jarvis, *keys):
    from jarvis.config import save_runtime_overrides
    try:
        save_runtime_overrides(jarvis.config, [f"voice.{k}" for k in keys])
    except Exception:
        pass  # a read-only config dir must not break the command


def _status_text(jarvis):
    voice = jarvis.config.get("voice", {})
    engine = make_engine(jarvis.config)
    ok, missing = engine.ready()
    name = voice.get("kokoro_voice") if voice.get("engine") == "kokoro" else voice.get("model")
    lines = [f"Auto-speak: {'on' if voice.get('enabled') else 'off'}",
             f"Engine: {voice.get('engine', 'piper')} ({name})",
             f"Ready: {'yes' if ok else 'no'}"]
    if missing:
        lines.append(f"Missing: {missing}  (run /voice download {missing})")
    return "\n".join(lines)


def handle_voice_command(jarvis, args) -> str:
    """Handle `/voice ...`. args is the text after /voice (str or list of words)."""
    words = args.split() if isinstance(args, str) else list(args or [])
    sub = words[0].lower() if words else "status"
    rest = words[1:]
    config = jarvis.config
    if sub == "status":
        return _status_text(jarvis)
    if sub in ("on", "off"):
        config["voice"]["enabled"] = sub == "on"
        _persist(jarvis, "enabled")
        jarvis.refresh_voice()
        text = f"Auto-speak {sub}."
        if sub == "on":
            ok, missing = make_engine(config).ready()
            if not ok:
                text += f" The voice is not ready yet; run /voice download {missing}."
        return text
    if sub == "engine":
        if not rest or rest[0] not in ("piper", "kokoro"):
            return "Usage: /voice engine <piper|kokoro>"
        config["voice"]["engine"] = rest[0]
        _persist(jarvis, "engine")
        jarvis.refresh_voice()
        return f"Voice engine set to {rest[0]}.\n" + _status_text(jarvis)
    if sub == "models":
        lines = []
        for item in models_manager.catalog(config):
            state = "installed" if item["installed"] else "not downloaded"
            pkg = "" if item["package_installed"] else f", needs pip package {item['package']}"
            lines.append(f"{item['id']:28} {item['size_mb']:>4} MB  {state}{pkg}  - {item['label']}")
        return "\n".join(lines)
    if sub == "download":
        if not rest:
            return "Usage: /voice download <item_id>  (see /voice models)"
        item_id = rest[0]
        last = {}

        def show(event):
            line = event["message"] or event["status"]
            fraction = event.get("progress")
            if event["status"] == "downloading" and fraction is not None:
                line = f"downloading {int(fraction * 100)}%"
            if line != last.get("line"):
                last["line"] = line
                print(f"[voice] {item_id}: {line}", flush=True)

        try:
            models_manager.download(item_id, show, config)
        except KeyError:
            return f"Unknown item {item_id}. See /voice models."
        except Exception as error:
            return f"Download failed: {error}"
        jarvis.refresh_voice()
        return f"{item_id} is ready."
    return _VOICE_HELP
