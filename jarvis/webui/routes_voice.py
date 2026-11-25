"""Voice routes: settings, text-to-speech for the browser, model downloads, catalog.

The browser plays the audio, so the server only synthesizes WAV bytes. The
voice/model modules are imported lazily so the web UI starts without them.
"""

import re
import threading

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .state import WebState, fail, web

router = APIRouter(prefix="/api")

MAX_TTS_CHARS = 5000
VOICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
ENGINES = ("piper", "kokoro")


class VoiceSettingsBody(BaseModel):
    auto_speak: bool | None = None
    engine: str | None = None
    piper_voice: str | None = None
    kokoro_voice: str | None = None


class TtsBody(BaseModel):
    text: str


class DownloadBody(BaseModel):
    item_id: str


def _catalog(state):
    try:
        from ..models_manager import catalog
    except ImportError:
        fail(501, "The model catalog is not available in this build")
    return catalog(state.jarvis.config)


@router.get("/voice")
def get_voice(state: WebState = Depends(web)):
    return state.voice_info()


@router.post("/voice/settings")
def post_voice_settings(body: VoiceSettingsBody, state: WebState = Depends(web)):
    from ..config import save_runtime_overrides
    voice = state.jarvis.config["voice"]
    changes = {}
    if body.auto_speak is not None:
        changes["enabled"] = body.auto_speak
    if body.engine is not None:
        if body.engine not in ENGINES:
            fail(400, f"engine must be one of: {', '.join(ENGINES)}")
        changes["engine"] = body.engine
    for field, key in (("piper_voice", "model"), ("kokoro_voice", "kokoro_voice")):
        value = getattr(body, field)
        if value is not None:
            if not VOICE_NAME_RE.match(value):
                fail(400, f"Invalid {field}")
            changes[key] = value
    voice.update(changes)
    save_runtime_overrides(state.jarvis.config, [f"voice.{key}" for key in changes])
    state.invalidate_tts()
    state.jarvis.refresh_voice()
    return state.voice_info()


@router.post("/voice/tts")
def post_voice_tts(body: TtsBody, state: WebState = Depends(web)):
    text = body.text.strip()
    if not text:
        fail(400, "Empty text")
    if len(text) > MAX_TTS_CHARS:
        fail(413, f"Text too long ({MAX_TTS_CHARS} characters max)")
    try:
        engine = state.tts_engine()
    except ImportError:
        fail(501, "Text-to-speech is not available in this build")
    ready, missing = engine.ready()
    if not ready:
        item = next((i for i in _safe_catalog(state) if i.get("id") == missing), {})
        return JSONResponse({"error": "Voice model not installed", "needs_download": missing,
                             "size_mb": item.get("size_mb"), "label": item.get("label")},
                            status_code=424)
    try:
        with state.synth_lock:  # sync route: FastAPI already runs it in a worker thread
            audio = engine.synthesize(text)
    except Exception as error:
        fail(500, f"Speech synthesis failed: {error}")
    return Response(audio, media_type="audio/wav")


def _safe_catalog(state):
    try:
        return _catalog(state)
    except Exception:
        return []


@router.post("/voice/download", status_code=202)
def post_voice_download(body: DownloadBody, state: WebState = Depends(web)):
    item_id = body.item_id.strip()
    if not any(item.get("id") == item_id for item in _catalog(state)):
        fail(400, "Unknown model")
    with state.download_lock:
        if item_id in state.active_downloads:
            fail(409, "This model is already downloading")
        state.active_downloads.add(item_id)
    threading.Thread(target=_download, args=(state, item_id), daemon=True).start()
    return {"started": True, "item_id": item_id}


def _download(state, item_id):
    from ..models_manager import download

    def report(update):
        data = {"item_id": item_id, "status": "downloading", "progress": None, "message": "",
                **(update if isinstance(update, dict) else {})}
        state.downloads[item_id] = data
        state.emit("voice_download", **data)

    try:
        download(item_id, progress=report, config=state.jarvis.config)
        if state.downloads.get(item_id, {}).get("status") != "done":
            report({"status": "done", "progress": 1.0})
    except Exception as error:
        report({"status": "error", "message": str(error)})
    finally:
        with state.download_lock:
            state.active_downloads.discard(item_id)
        state.invalidate_tts()  # a newly installed model makes the cached engine stale


@router.get("/models/catalog")
def get_models_catalog(state: WebState = Depends(web)):
    return _catalog(state)
