"""Model management — download TTS/STT models from Hugging Face or use local drops."""

import os
import hashlib
from pathlib import Path
from urllib.request import urlretrieve
from urllib.error import URLError
import json

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

PIPER_MODELS = {
    "en_US-lessac-medium": {
        "repo": "rhasspy/piper-voices",
        "files": [
            "en/en_US/lessac/medium/en_US-lessac-medium.onnx",
            "en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
        ],
        "subdir": "piper/en_US-lessac-medium",
    },
    "en_US-amy-low": {
        "repo": "rhasspy/piper-voices",
        "files": [
            "en/en_US/amy/low/en_US-amy-low.onnx",
            "en/en_US/amy/low/en_US-amy-low.onnx.json",
        ],
        "subdir": "piper/en_US-amy-low",
    },
    "en_US-ryan-high": {
        "repo": "rhasspy/piper-voices",
        "files": [
            "en/en_US/ryan/high/en_US-ryan-high.onnx",
            "en/en_US/ryan/high/en_US-ryan-high.onnx.json",
        ],
        "subdir": "piper/en_US-ryan-high",
    },
}

KOKORO_MODELS = {
    "hexgrad/Kokoro-82M": {
        "repo": "hexgrad/Kokoro-82M",
        "files": ["kokoro-v0.19.onnx", "voices.json"],
        "subdir": "kokoro/v0.19",
    },
}


class ModelManager:
    def __init__(self, models_dir=None):
        self.models_dir = Path(models_dir) if models_dir else MODELS_DIR
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def _hf_url(self, repo, file):
        return f"https://huggingface.co/{repo}/resolve/main/{file}"

    def _download(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return True
        try:
            print(f"[models] Downloading {dest.name}...")
            urlretrieve(url, dest)
            return True
        except URLError as e:
            print(f"[models] Download failed: {e}")
            return False

    def ensure_piper(self, model_name="en_US-lessac-medium"):
        """Ensure Piper TTS model is available. Returns (onnx_path, config_path) or (None, None)."""
        if model_name not in PIPER_MODELS:
            print(f"[models] Unknown Piper model: {model_name}")
            return None, None

        info = PIPER_MODELS[model_name]
        model_dir = self.models_dir / info["subdir"]
        model_dir.mkdir(parents=True, exist_ok=True)

        onnx_path = model_dir / info["files"][0].split("/")[-1]
        json_path = model_dir / info["files"][1].split("/")[-1]

        for file in info["files"]:
            dest = model_dir / file.split("/")[-1]
            if not dest.exists():
                url = self._hf_url(info["repo"], file)
                self._download(url, dest)

        return (onnx_path if onnx_path.exists() else None,
                json_path if json_path.exists() else None)

    def ensure_kokoro(self, model_name="hexgrad/Kokoro-82M"):
        """Ensure Kokoro TTS model is available. Returns (onnx_path, voices_json_path) or (None, None)."""
        if model_name not in KOKORO_MODELS:
            return None, None

        info = KOKORO_MODELS[model_name]
        model_dir = self.models_dir / info["subdir"]
        model_dir.mkdir(parents=True, exist_ok=True)

        onnx_path = model_dir / "kokoro-v0.19.onnx"
        voices_path = model_dir / "voices.json"

        for file in info["files"]:
            dest = model_dir / file
            if not dest.exists():
                url = self._hf_url(info["repo"], file)
                self._download(url, dest)

        return (onnx_path if onnx_path.exists() else None,
                voices_path if voices_path.exists() else None)

    def ensure_whisper(self, model_name="small"):
        """Ensure faster-whisper model is available. Returns model_dir or None."""
        model_dir = self.models_dir / "whisper" / model_name
        model_dir.mkdir(parents=True, exist_ok=True)

        try:
            from faster_whisper import download_model
            download_model(model_name, cache_dir=str(self.models_dir))
            return str(model_dir)
        except ImportError:
            print("[models] faster-whisper not installed")
            return None
        except Exception as e:
            print(f"[models] Whisper download failed: {e}")
            return None

    def list_local(self):
        """List all locally available models."""
        models = {"piper": [], "kokoro": [], "whisper": []}
        if not self.models_dir.exists():
            return models

        for d in self.models_dir.iterdir():
            if d.is_dir():
                if d.name.startswith("piper"):
                    models["piper"].append(d.name)
                elif d.name.startswith("kokoro"):
                    models["kokoro"].append(d.name)
        return models