"""Generic routes for runtime sessions (agent chats, discussions, routine runs).

Every feature reuses these: send a message, cancel, read history, retry. The
instance is looked up by session id in `state.runtimes`.
"""

from fastapi import APIRouter, Depends

from .history import render_history
from .routes_core import ChatBody
from .state import WebState, check_id, fail, web

router = APIRouter(prefix="/api/runtime")


def _runtime(state, session_id):
    check_id(session_id, "session id")
    jarvis = state.runtimes.get(session_id)
    if jarvis is None:
        fail(404, "Session not found")
    return jarvis


@router.post("/{session_id}/chat", status_code=202)
def runtime_chat(session_id: str, body: ChatBody, state: WebState = Depends(web)):
    _runtime(state, session_id)
    text = body.text.strip()
    if not text:
        fail(400, "Empty message")
    if not state.runtimes.start_turn(session_id, text):
        fail(409, "A turn is already running")
    return {"started": True}


@router.post("/{session_id}/cancel")
def runtime_cancel(session_id: str, state: WebState = Depends(web)):
    _runtime(state, session_id)
    state.runtimes.cancel(session_id)
    return {"cancelled": True}


@router.get("/{session_id}/history")
def runtime_history(session_id: str, state: WebState = Depends(web)):
    jarvis = _runtime(state, session_id)
    return {"session": jarvis.sessions.get(session_id) or {"id": session_id},
            "history": render_history(jarvis.history, state.artifacts.get),
            "busy": state.runtimes.busy(session_id)}


@router.post("/{session_id}/retry", status_code=202)
def runtime_retry(session_id: str, state: WebState = Depends(web)):
    jarvis = _runtime(state, session_id)
    if not jarvis.last_user_message():
        fail(400, "Nothing to retry")
    if not state.runtimes.start_turn(session_id, None):
        fail(409, "A turn is already running")
    return {"started": True}
