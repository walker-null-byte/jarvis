import os
import re
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
        # Target sample rate for playback (pulseaudio/pipewire default)
        self._target_sr = 44100
        self._speak_thread = None
        self._audio_device = config.get("voice", {}).get("audio_device") or None

    def set_amplitude_callback(self, cb):
        self._amplitude_cb = cb

    def _emit_amplitude(self, amp):
        if self._amplitude_cb:
            self._amplitude_cb(amp)

    @staticmethod
    def _measure_amplitude(audio):
        """Return a stable 0..1 level from an int16 audio frame."""
        values = np.asarray(audio, dtype=np.float32)
        if values.size == 0:
            return 0.0
        values = values / 32768.0
        peak = float(np.max(np.abs(values)))
        rms = float(np.sqrt(np.mean(values * values)))
        return max(0.0, min(1.0, rms * 4.0 + peak * 0.15))

    def speak(self, text: str):
        raise NotImplementedError

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

    def _resample(self, audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        """Simple linear interpolation resampling."""
        if src_sr == dst_sr:
            return audio
        ratio = dst_sr / src_sr
        new_len = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_len)
        return np.interp(indices, np.arange(len(audio)), audio).astype(np.int16)

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
    """No-op voice used when voice is disabled or unavailable."""

    def speak(self, text: str):
        self._emit_amplitude(0.0)


class PiperVoice(Voice):
    """Piper TTS - fast, lightweight, good quality."""

    def __init__(self, config):
        super().__init__(config)
        self._onnx = None
        self._config = None
        self._voice = None

    def _ensure_model(self):
        model_name = self.config["voice"]["model"]
        if self._voice is not None:
            return True
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
        text = self.speech_text(text)
        if not text:
            return
        if not self._ensure_model():
            return

        self._speaking = True
        try:
            # Split into sentences for streaming
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
            for sent in sentences:
                if not self._speaking:
                    break
                for chunk in self._voice.synthesize(sent):
                    audio = chunk.audio_int16_bytes
                    self._emit_amplitude(self._measure_amplitude(np.frombuffer(audio, dtype=np.int16)))
                    self._play_audio(audio)
        finally:
            self._speaking = False
            self._emit_amplitude(0.0)

    def _play_audio(self, audio_bytes):
        try:
            import sounddevice as sd
            audio = np.frombuffer(audio_bytes, dtype=np.int16)
            src_sr = self._voice.config.sample_rate
            # Resample to target sample rate
            audio = self._resample(audio, src_sr, self._target_sr)
            kwargs = {"samplerate": self._target_sr}
            if self._audio_device:
                kwargs["device"] = self._audio_device
            sd.play(audio, **kwargs)
            sd.wait()
        except Exception:
            import tempfile
            import subprocess
            import wave
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                path = f.name
            try:
                with wave.open(path, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(self._target_sr)
                    wav.writeframes(audio_bytes)
                subprocess.run(["aplay", "-q", path], capture_output=True, check=False)
            finally:
                os.unlink(path)


class KokoroVoice(Voice):
    """Kokoro TTS - high quality, multiple voices."""

    def __init__(self, config):
        super().__init__(config)
        self._session = None
        self._voices = None

    def _ensure_model(self):
        model_name = self.config["voice"]["kokoro_model"]
        if self._session is not None and self._voices is not None:
            return True
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
        text = self.speech_text(text)
        if not text:
            return
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

            self._play_audio(audio.tobytes())
        finally:
            self._speaking = False
            self._emit_amplitude(0.0)

    def _play_audio(self, audio_bytes):
        try:
            import sounddevice as sd
            audio = np.frombuffer(audio_bytes, dtype=np.int16)
            kwargs = {"samplerate": 24000, "channels": 1, "dtype": "int16"}
            if self._audio_device:
                kwargs["device"] = self._audio_device
            frame_size = max(1, int(kwargs["samplerate"] * 0.04))
            with sd.OutputStream(**kwargs) as stream:
                for start in range(0, len(audio), frame_size):
                    frame = audio[start:start + frame_size]
                    self._emit_amplitude(self._measure_amplitude(frame))
                    stream.write(frame)
        except Exception:
            import tempfile
            import subprocess
            import wave
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                path = f.name
            try:
                with wave.open(path, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(audio_bytes)
                self._emit_amplitude(self._measure_amplitude(np.frombuffer(audio_bytes, dtype=np.int16)))
                subprocess.run(["aplay", "-q", path], capture_output=True, check=False)
            finally:
                os.unlink(path)


def get_voice(config):
    """Factory function to get the configured TTS engine."""
    voice_config = config.get("voice", {})
    if not voice_config.get("enabled", False):
        return NullVoice(config)
    engine = voice_config.get("engine", "piper")
    if engine == "kokoro":
        return KokoroVoice(config)
    return PiperVoice(config)
