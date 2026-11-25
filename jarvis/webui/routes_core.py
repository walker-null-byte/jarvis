"""Core routes: status, chat, events (SSE), approvals, sessions, voice, artifacts."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..commands import SLASH_COMMANDS, SLASH_SUBCOMMANDS, handle_slash_command
from .hub import sse_format
from .state import WebState, check_id, fail, web

router = APIRouter(prefix="/api")

HEARTBEAT_SECONDS = 15
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class ChatBody(BaseModel):
    text: str


class ApprovalBody(BaseModel):
    approved: bool


class TitleBody(BaseModel):
    title: str


# ---------- status / commands ----------

@router.get("/status")
def get_status(state: WebState = Depends(web)):
    return state.status()


@router.get("/snapshot")
def get_snapshot(state: WebState = Depends(web)):
    return state.snapshot()


@router.get("/commands")
def get_commands(state: WebState = Depends(web)):
    return {"commands": [
        {"name": name, "description": description,
         "subcommands": [{"name": sub, "description": text}
                         for sub, text in SLASH_SUBCOMMANDS.get(name, ())]}
        for name, description in SLASH_COMMANDS
    ]}


# ---------- chat ----------

@router.post("/chat", status_code=202)
def post_chat(body: ChatBody, state: WebState = Depends(web)):
    text = body.text.strip()
    if not text:
        fail(400, "Empty message")
    if text.startswith("/"):
        output = handle_slash_command(state.jarvis, text)
        return JSONResponse({"command_output": output or ""}, status_code=200)
    if not state.start_turn(text):
        fail(409, "A turn is already running")
    return {"started": True}


@router.post("/cancel")
def post_cancel(state: WebState = Depends(web)):
    state.jarvis.cancel()
    return {"cancelled": True}


@router.post("/retry", status_code=202)
def post_retry(state: WebState = Depends(web)):
    if state.busy:
        fail(409, "A turn is already running")
    if not state.jarvis.last_user_message():
        fail(400, "Nothing to retry")
    if not state.start_turn(None):
        fail(409, "A turn is already running")
    return {"started": True}


@router.post("/messages/{index}/edit", status_code=202)
def post_edit_message(index: int, body: ChatBody, state: WebState = Depends(web)):
    text = body.text.strip()
    if not text:
        fail(400, "Empty message")
    if state.busy:
        fail(409, "A turn is already running")
    history = state.jarvis.history
    if not 0 <= index < len(history) or history[index].get("role") != "user":
        fail(400, "Only your own messages can be edited")
    if not state.start_action(lambda: state.jarvis.edit_message(index, text)):
        fail(409, "A turn is already running")
    return {"started": True}


@router.post("/regenerate", status_code=202)
def post_regenerate(state: WebState = Depends(web)):
    if state.busy:
        fail(409, "A turn is already running")
    if not state.jarvis.last_user_message():
        fail(400, "Nothing to regenerate")
    if not state.start_action(state.jarvis.regenerate):
        fail(409, "A turn is already running")
    return {"started": True}


@router.post("/approvals/{request_id}")
def post_approval(request_id: str, body: ApprovalBody, state: WebState = Depends(web)):
    if not state.jarvis.approvals.resolve(request_id, body.approved):
        fail(404, "No such pending approval")
    return {"resolved": True, "approved": body.approved}


# ---------- live events ----------

@router.get("/events")
async def get_events(request: Request, state: WebState = Depends(web)):
    client = state.hub.connect()  # connect first so nothing is missed after the snapshot

    async def stream():
        try:
            snapshot = await asyncio.to_thread(state.snapshot)
            yield sse_format({
                "kind": "snapshot", "session_id": state.jarvis.session_id, "turn_id": "",
                "timestamp": "", "data": snapshot})
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(client.queue.get(), HEARTBEAT_SECONDS)
                    yield sse_format(message)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            state.hub.disconnect(client)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})


# ---------- sessions ----------

def _require_idle(state):
    if state.busy:
        fail(409, "Finish or cancel the running turn first")


@router.get("/sessions")
def list_sessions(q: str = "", state: WebState = Depends(web)):
    sessions = state.jarvis.sessions.search(q, limit=100) if q.strip() else state.jarvis.sessions.list()
    active = state.jarvis.session_id
    # A brand-new session isn't on disk until its first message; still show it.
    if not q.strip() and all(item.get("id") != active for item in sessions):
        current = state.jarvis.sessions.get(active)
        if current:
            sessions = [current, *sessions]
    return {"sessions": [{**item, "active": item.get("id") == active} for item in sessions],
            "active": active}


@router.post("/sessions")
def new_session(state: WebState = Depends(web)):
    _require_idle(state)
    state.jarvis.new_session()
    return state.snapshot()


@router.post("/sessions/{session_id}/resume")
def resume_session(session_id: str, state: WebState = Depends(web)):
    check_id(session_id, "session id")
    _require_idle(state)
    result = state.jarvis.resume_session(session_id)
    if result.get("error"):
        fail(404, result["error"])
    return state.snapshot()


@router.patch("/sessions/{session_id}")
def rename_session(session_id: str, body: TitleBody, state: WebState = Depends(web)):
    check_id(session_id, "session id")
    meta = state.jarvis.sessions.rename(session_id, body.title)
    if meta is None:
        fail(404, "Session not found")
    return meta


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, state: WebState = Depends(web)):
    check_id(session_id, "session id")
    active = session_id == state.jarvis.session_id
    if active:
        _require_idle(state)
    if not state.jarvis.sessions.delete(session_id):
        fail(404, "Session not found")
    if active:
        state.jarvis.new_session()
    return {"deleted": session_id, "active": state.jarvis.session_id}


@router.get("/sessions/{session_id}/export")
def export_session(session_id: str, format: str = "markdown", state: WebState = Depends(web)):
    check_id(session_id, "session id")
    try:
        content = state.jarvis.sessions.export(session_id, format=format)
    except ValueError as error:
        fail(404 if "not found" in str(error) else 400, str(error))
    extension, media = ("json", "application/json") if format.lower() == "json" else ("md", "text/markdown")
    return Response(content, media_type=f"{media}; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="session-{session_id}.{extension}"'})


# ---------- voice (server-side microphone) ----------

def _listener(state):
    if state.listener is None:
        try:
            from ..listen import Listener
            state.listener = Listener(state.jarvis.config)
        except ImportError as error:
            fail(501, f"Voice input needs the voice extras: {error}")
    return state.listener


@router.post("/voice/listen/start")
def listen_start(state: WebState = Depends(web)):
    listener = _listener(state)
    try:
        started = listener.start()
    except Exception as error:
        fail(501, f"Microphone unavailable: {error}")
    if started is False:
        fail(501, "Voice input unavailable: download a speech-recognition model in the "
                  "Voice tab (or /voice download whisper:base) and check your microphone.")
    state.emit("voice_listening", active=True)
    return {"listening": True}


@router.post("/voice/listen/stop")
def listen_stop(state: WebState = Depends(web)):
    listener = _listener(state)
    try:
        transcript = listener.stop()
    except Exception as error:
        fail(500, f"Transcription failed: {error}")
    finally:
        state.emit("voice_listening", active=False)
    return {"transcript": transcript or ""}


# ---------- artifacts ----------

def _artifact_id(value):
    check_id(value, "artifact id")
    if not value.startswith("artifact_"):
        fail(400, "Invalid artifact id")
    return value


@router.get("/artifacts")
def list_artifacts(state: WebState = Depends(web)):
    return state.artifacts.list()


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, state: WebState = Depends(web)):
    item = state.artifacts.get(_artifact_id(artifact_id))
    if not item:
        fail(404, "Artifact not found")
    return item


@router.get("/artifacts/{artifact_id}/file")
def get_artifact_file(artifact_id: str, state: WebState = Depends(web)):
    item = state.artifacts.get(_artifact_id(artifact_id))
    if not item:
        fail(404, "Artifact not found")
    path = Path(item.get("path") or "").expanduser()
    if item.get("type") != "image" or path.suffix.lower() not in IMAGE_SUFFIXES:
        fail(404, "No image file for this artifact")
    if not path.is_file():
        fail(404, "File not found")
    return FileResponse(path)
