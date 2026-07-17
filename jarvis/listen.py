"""STT — push-to-talk using faster-whisper."""

import os
import threading
import tempfile
import numpy as np
from pathlib import Path

from jarvis.models_manager import ModelManager


class Listener:
    """Push-to-talk speech recognition using faster-whisper."""

    def __init__(self, config):
        self.config = config
        voice_cfg = config.get("voice", {})
        self.model_name = voice_cfg.get("stt_model", "small")
        self.device = voice_cfg.get("stt_device", "auto")
        self.language = voice_cfg.get("stt_language")
        self.sample_rate = 16000

        models_dir = config["paths"].get("models") or "models"
        self.models = ModelManager(models_dir)
        self._model = None
        self._recording = False
        self._audio_chunks = []
        self._stream = None

    def _load_model(self):
        if self._model is None:
            model_dir = self.models.ensure_whisper(self.model_name)
            if not model_dir:
                raise RuntimeError(f"Whisper model {self.model_name} not available")
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                model_dir,
                device=self.device,
                compute_type=self.compute_type,
            )

    def start(self):
        """Start recording. Call on key press."""
        if self._recording:
            return

        self._load_model()
        self._audio_chunks = []
        self._recording = True

        def callback(indata, frames, time, status):
            if self._recording:
                self._audio_chunks.append(indata.copy())

        try:
            import sounddevice as sd
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                callback=callback,
            )
            self._stream.start()
        except Exception as e:
            print(f"[listen] Failed to start recording: {e}")
            self._recording = False

    def stop(self):
        """Stop recording and return transcribed text."""
        if not self._recording:
            return ""

        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if not self._audio_chunks:
            return ""

        audio = np.concatenate(self._audio_chunks, axis=0).flatten()

        try:
            segments, _ = self._model.transcribe(
                audio,
                language=self.language,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            text = " ".join(seg.text for seg in segments).strip()
            return text
        except Exception as e:
            print(f"[listen] Transcription error: {e}")
            return ""