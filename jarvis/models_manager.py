"""Voice / embedding model catalog and downloader.

Models are never bundled. The user clicks a button (web UI Models panel or
`/voice download <id>`), which calls `download(item_id, progress)`.

Layout under the models dir (gitignored):
  piper/<voice>/<voice>.onnx + .onnx.json
  kokoro/v1.0/kokoro-v1.0.int8.onnx + voices-v1.0.bin
  whisper/<size>/model.bin config.json tokenizer.json vocabulary.txt
The embedding model lives in the normal Hugging Face cache so model2vec
finds it by repo id.
"""

import importlib
import importlib.util
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

from jarvis.config import ROOT

MODELS_DIR = ROOT / "models"

HF = "https://huggingface.co"
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
EMBEDDINGS_REPO = "minishlab/potion-base-8M"
KOKORO_DIR = "kokoro/v1.0"
KOKORO_FILES = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")

# (voice name, path inside rhasspy/piper-voices, label, onnx bytes, json bytes)
# Sizes verified with HEAD requests.
_PIPER_VOICES = [
    ("en_US-lessac-medium", "en/en_US/lessac/medium", "Lessac (US, medium)", 63201294, 4885),
    ("en_US-amy-medium", "en/en_US/amy/medium", "Amy (US, medium)", 63221984, 6289),
    ("en_GB-alan-medium", "en/en_GB/alan/medium", "Alan (UK, medium)", 63201294, 4888),
    ("en_US-ryan-high", "en/en_US/ryan/high", "Ryan (US, high quality)", 120786792, 4166),
]
_WHISPER = {  # size -> model.bin bytes (config/tokenizer/vocabulary add ~2.7 MB)
    "tiny": 75538270,
    "base": 145217532,
    "small": 483546902,
}
_WHISPER_FILES = {"config.json": 2400, "tokenizer.json": 2203239, "vocabulary.txt": 459861}
# minishlab/potion-base-8M files model2vec needs (name -> bytes)
_EMBEDDING_FILES = {
    "config.json": 202,
    "model.safetensors": 30236760,
    "modules.json": 278,
    "tokenizer.json": 683666,
}

# Items that need no files in the models dir to be "installed".
# kind, engine, label, package (pip name), import name
_PACKAGES = {
    "piper": ("piper-tts", "piper"),
    "kokoro": ("kokoro-onnx", "kokoro_onnx"),
    "whisper": ("faster-whisper", "faster_whisper"),
    "embeddings": ("model2vec", "model2vec"),
}
# kokoro-onnx declares python<3.14 but works on newer interpreters; pip must be told.
_PIP_EXTRA = {"kokoro-onnx": ["--ignore-requires-python"]}


def models_dir(config=None):
    """Models directory for a config (relative paths resolve against the repo root)."""
    raw = (config or {}).get("paths", {}).get("models") or ""
    path = Path(raw) if raw else MODELS_DIR
    return path if path.is_absolute() else ROOT / path


# ---------- item specs ----------

def _item_specs():
    """All catalog items: id -> dict(kind, engine, label, package, files).

    files is a list of (url, relative destination, size_bytes). For embeddings
    the list holds (filename, filename, size) and is fetched via huggingface_hub.
    """
    items = {}
    for name, path, label, onnx_size, json_size in _PIPER_VOICES:
        base = f"{HF}/rhasspy/piper-voices/resolve/main/{path}/{name}"
        items[f"piper:{name}"] = {
            "kind": "tts", "engine": "piper", "label": f"Piper: {label}", "package": "piper",
            "files": [(f"{base}.onnx", f"piper/{name}/{name}.onnx", onnx_size),
                      (f"{base}.onnx.json", f"piper/{name}/{name}.onnx.json", json_size)],
        }
    items["kokoro:v1.0"] = {
        "kind": "tts", "engine": "kokoro", "label": "Kokoro v1.0 (int8, many voices)",
        "package": "kokoro",
        "files": [(f"{KOKORO_RELEASE}/{KOKORO_FILES[0]}", f"{KOKORO_DIR}/{KOKORO_FILES[0]}", 92361271),
                  (f"{KOKORO_RELEASE}/{KOKORO_FILES[1]}", f"{KOKORO_DIR}/{KOKORO_FILES[1]}", 28214398)],
    }
    for size, model_bytes in _WHISPER.items():
        base = f"{HF}/Systran/faster-whisper-{size}/resolve/main"
        files = [(f"{base}/model.bin", f"whisper/{size}/model.bin", model_bytes)]
        files += [(f"{base}/{n}", f"whisper/{size}/{n}", b) for n, b in _WHISPER_FILES.items()]
        items[f"whisper:{size}"] = {
            "kind": "stt", "engine": "whisper", "label": f"Whisper {size} (speech to text)",
            "package": "whisper", "files": files,
        }
    items["embeddings:potion-base-8M"] = {
        "kind": "embeddings", "engine": "model2vec", "label": "Tool-search embeddings (potion-base-8M)",
        "package": "embeddings",
        "files": [(n, n, b) for n, b in _EMBEDDING_FILES.items()],
    }
    return items


def _spec(item_id):
    spec = _item_specs().get(item_id)
    if spec is None:
        raise KeyError(f"unknown model item: {item_id}")
    return spec


def _package_installed(group):
    return importlib.util.find_spec(_PACKAGES[group][1]) is not None


def _file_ok(path):
    return path.is_file() and path.stat().st_size > 0


def _embeddings_cached():
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    for name in _EMBEDDING_FILES:
        if name == "modules.json":
            continue  # optional in older repo revisions
        if not isinstance(try_to_load_from_cache(EMBEDDINGS_REPO, name), str):
            return False
    return True


def is_installed(item_id, config=None):
    """True when the item's model FILES are present (the pip package is separate)."""
    spec = _spec(item_id)
    if spec["kind"] == "embeddings":
        return _embeddings_cached()
    root = models_dir(config)
    if spec["engine"] == "whisper":
        folder = root / "whisper" / item_id.split(":", 1)[1]
        # A user may also drop in any CTranslate2 model folder.
        return _file_ok(folder / "model.bin") and _file_ok(folder / "config.json")
    return all(_file_ok(root / dest) for _url, dest, _size in spec["files"])


def catalog(config=None):
    """List every downloadable item with install state."""
    voice = (config or {}).get("voice", {})
    selected = {f"piper:{voice.get('model', 'en_US-lessac-medium')}",
                f"whisper:{voice.get('stt_model', 'small')}", "embeddings:potion-base-8M"}
    out = []
    for item_id, spec in _item_specs().items():
        group = spec["package"]
        total = sum(size for _u, _d, size in spec["files"])
        out.append({
            "id": item_id,
            "kind": spec["kind"],
            "engine": spec["engine"],
            "label": spec["label"],
            "size_mb": max(1, round(total / 1_000_000)),
            "installed": is_installed(item_id, config),
            "package": _PACKAGES[group][0],
            "package_installed": _package_installed(group),
            "default": item_id in selected,
        })
    return out


# ---------- download ----------

def _open_url(url):
    """Open a URL for streaming. Tests monkeypatch this with a fake transport."""
    return urlopen(Request(url, headers={"User-Agent": "jarvis-model-download"}), timeout=60)


def _hf_download(repo, filename):
    """Download one file into the standard Hugging Face cache. Monkeypatched in tests."""
    from huggingface_hub import hf_hub_download
    return hf_hub_download(repo, filename)


def _run_pip(package):
    """Install a pip package into the running interpreter's environment."""
    cmd = [sys.executable, "-m", "pip", "install", package, *_PIP_EXTRA.get(package, [])]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and "No module named pip" in (result.stderr or ""):
        uv = shutil.which("uv")  # uv-created venvs ship without pip
        if uv:
            cmd = [uv, "pip", "install", "--python", sys.executable, package,
                   *_PIP_EXTRA.get(package, [])]
            result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"pip install {package} failed: {' '.join(tail)}")
    importlib.invalidate_caches()


def _stream_to(url, dest, on_bytes):
    """Stream url to dest via a .part file, then rename (atomic)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with _open_url(url) as response, open(part, "wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                on_bytes(len(chunk))
        if part.stat().st_size == 0:
            raise RuntimeError(f"empty download: {url}")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def download(item_id, progress=None, config=None):
    """Install the item's pip package (if missing) and download its files.

    progress(dict(item_id, status, progress, message)); status is one of
    installing | downloading | done | error. Raises after reporting "error".
    """
    last_emit = [0.0]

    def report(status, fraction=None, message="", force=True):
        now = time.monotonic()
        if not force and now - last_emit[0] < 0.2:
            return
        last_emit[0] = now
        if progress:
            progress({"item_id": item_id, "status": status, "progress": fraction, "message": message})

    try:
        spec = _spec(item_id)
        group = spec["package"]
        package = _PACKAGES[group][0]
        if not _package_installed(group):
            report("installing", None, f"Installing {package}...")
            _run_pip(package)
            if not _package_installed(group):
                raise RuntimeError(f"{package} installed but cannot be imported")
        if is_installed(item_id, config):
            report("done", 1.0, "Already installed.")
            return
        files = spec["files"]
        total = sum(size for _u, _d, size in files) or 1
        done = [0]
        report("downloading", 0.0, f"Downloading {spec['label']}...")

        def on_bytes(count):
            done[0] += count
            report("downloading", min(0.99, done[0] / total), "", force=False)

        if spec["kind"] == "embeddings":
            for name, _dest, size in files:
                _hf_download(EMBEDDINGS_REPO, name)
                done[0] += size
                report("downloading", min(0.99, done[0] / total), f"Fetched {name}")
        else:
            root = models_dir(config)
            for url, dest, size in files:
                target = root / dest
                if _file_ok(target):
                    done[0] += size
                    continue
                _stream_to(url, target, on_bytes)
        report("done", 1.0, f"{spec['label']} is ready.")
    except Exception as error:
        report("error", None, str(error))
        raise


# ---------- local lookups used by voice.py / listen.py (never download) ----------

class ModelManager:
    def __init__(self, models_dir_path=None):
        self.models_dir = Path(models_dir_path) if models_dir_path else MODELS_DIR

    def ensure_piper(self, model_name="en_US-lessac-medium"):
        """Return (onnx_path, json_path) when the voice is downloaded, else (None, None)."""
        folder = self.models_dir / "piper" / model_name
        onnx, meta = folder / f"{model_name}.onnx", folder / f"{model_name}.onnx.json"
        if _file_ok(onnx) and _file_ok(meta):
            return onnx, meta
        return None, None

    def ensure_kokoro(self):
        """Return (model_path, voices_path) when Kokoro v1.0 is downloaded, else (None, None)."""
        folder = self.models_dir / KOKORO_DIR
        model, voices = folder / KOKORO_FILES[0], folder / KOKORO_FILES[1]
        if _file_ok(model) and _file_ok(voices):
            return model, voices
        return None, None

    def ensure_whisper(self, model_name="small"):
        """Return the model folder when downloaded (or user-provided), else None."""
        folder = self.models_dir / "whisper" / model_name
        if _file_ok(folder / "model.bin") or any(folder.glob("*.bin")):
            return str(folder)
        return None

    def list_local(self):
        """List locally available models."""
        found = {"piper": [], "kokoro": [], "whisper": []}
        for key in found:
            base = self.models_dir / key
            if base.is_dir():
                found[key] = sorted(str(p.relative_to(self.models_dir)) for p in base.iterdir() if p.is_dir())
        return found
