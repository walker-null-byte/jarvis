import os
import sys
import threading
import queue
import numpy as np
from pathlib import Path

from jarvis.models_manager import ModelManager
from jarvis.config import load_config, ROOT


class Voice:
    """Base TTS engine with amplitude callback."""

    def __init__(self, config):
        self.config = config
        models_dir = config["paths"].get("models") or str(ROOT / "models")
        self.models = ModelManager(models_dir)
        self._amplitude_cb = None
        self._speaking = False

    def set_amplitude_callback(self, cb):
        self._amplitude_cb = cb

    def _emit_amplitude(self, amp):
        if self._amplitude_cb:
            self._amplitude_cb(amp)

    def speak(self, text: str):
        raise NotImplementedError

    def stop(self):
        self._speaking = False

    @property
    def is_speaking(self):
        return self._speaking


class PiperVoice(Voice):
    """Piper TTS - fast, lightweight, good quality."""

    def __init__(self, config):
        super().__init__(config)
        self._onnx = None
        self._config = None
        self._voice = None

    def _ensure_model(self):
        model_name = self.config["voice"]["model"]
        onnx_path, json_path = self.models.ensure_piper(model_name)
        if not onnx_path or not json_path:
            return False

        try:
            import piper
            self._voice = piper.PiperVoice.load(str(onnx_path), config_path=str(json_path))
            return True
        except ImportError:
            print("[voice] Piper not installed: pip install piper-tts")
            return False
        except Exception as e:
            print(f"[voice] Piper load failed: {e}")
            return False

    def speak(self, text: str):
        if not self._ensure_model():
            return

        self._speaking = True
        try:
            # Split into sentences for streaming
            sentences = [s.strip() for s in text.split(".") if s.strip()]
            for sent in sentences:
                if not self._speaking:
                    break
                for chunk in self._voice.synthesize(sent):
                    audio = chunk.audio_int16_bytes
                    amp = float(np.abs(np.frombuffer(audio, dtype=np.int16)).max()) / 32768.0
                    self._emit_amplitude(amp)
                    self._play_audio(audio)
        finally:
            self._speaking = False
            self._emit_amplitude(0.0)

    def _play_audio(self, audio_bytes):
        # Use simple sounddevice for playback
        try:
            import sounddevice as sd
            audio = np.frombuffer(audio_bytes, dtype=np.int16)
            sd.play(audio, samplerate=self._voice.config.sample_rate)
            sd.wait()
        except ImportError:
            # Fallback: write to temp file and use system player
            import tempfile
            import subprocess
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                path = f.name
            subprocess.run(["aplay", "-q", path], capture_output=True)
            os.unlink(path)


class KokoroVoice(Voice):
    """Kokoro TTS - high quality, multiple voices."""

    def __init__(self, config):
        super().__init__(config)
        self._session = None
        self._voices = None

    def _ensure_model(self):
        model_name = self.config["voice"]["kokoro_model"]
        onnx_path, voices_path = self.models.ensure_kokoro(model_name)
        if not onnx_path or not voices_path:
            return False

        try:
            import onnxruntime as ort
            import json
            self._session = ort.InferenceSession(str(onnx_path))
            with open(voices_path) as f:
                self._voices = json.load(f)
            return True
        except ImportError:
            print("[voice] onnxruntime not installed: pip install onnxruntime")
            return False
        except Exception as e:
            print(f"[voice] Kokoro load failed: {e}")
            return False

    def speak(self, text: str):
        if not self._ensure_model():
            return

        self._speaking = True
        try:
            voice_name = self.config["voice"]["kokoro_voice"]
            voice = self._voices.get(voice_name)
            if not voice:
                print(f"[voice] Unknown Kokoro voice: {voice_name}")
                return

            import numpy as np
            # Kokoro inference
            inputs = {
                "text": np.array([text], dtype=np.object_),
                "voice": np.array(voice, dtype=np.float32),
                "speed": np.array([1.0], dtype=np.float32),
            }
            audio = self._session.run(None, inputs)[0]
            audio = (audio * 32767).astype(np.int16)

            # Emit amplitude
            amp = float(np.abs(audio).max()) / 32768.0
            self._emit_amplitude(amp)

            self._play_audio(audio.tobytes())
        finally:
            self._speaking = False
            self._emit_amplitude(0.0)

    def _play_audio(self, audio_bytes):
        try:
            import sounddevice as sd
            audio = np.frombuffer(audio_bytes, dtype=np.int16)
            sd.play(audio, samplerate=24000)
            sd.wait()
        except ImportError:
            import tempfile
            import subprocess
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                path = f.name
            subprocess.run(["aplay", "-q", path], capture_output=True)
            os.unlink(path)


def get_voice(config):
    """Factory function to get the configured TTS engine."""
    engine = config["voice"]["engine"]
    if engine == "kokoro":
        return KokoroVoice(config)
    return PiperVoice(config)