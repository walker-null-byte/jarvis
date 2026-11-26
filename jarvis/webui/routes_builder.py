"""Agent Builder routes: drafts made and edited with AI (docs/agent-builder.md).

The work happens in `state.builder` (jarvis/specialists/builder.py); these handlers translate
HTTP. Errors use the same {"error": message, ...extra} JSON as the agents routes (`issues` lists
the problems that block staging a draft). Installing a staged draft uses the existing
POST /api/agents/{id}/install.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..specialists import AgentError
from .routes_agents import friendly
from .state import WebState, check_id, web

router = APIRouter(prefix="/api")


class StartBody(BaseModel):
    request: str = ""
    name: str = ""
    base: str | None = None
    llm: dict | None = None


class ChatBody(BaseModel):
    text: str


class LlmBody(BaseModel):
    provider: str = ""
    model: str = ""


class EditBody(BaseModel):
    request: str = ""


@router.get("/agent-drafts")
@friendly
def list_drafts(state: WebState = Depends(web)):
    return {"drafts": state.builder.list()}


@router.post("/agent-drafts")
@friendly
def start_draft(body: StartBody, state: WebState = Depends(web)):
    base = check_id(body.base, "agent id") if body.base else None
    return state.builder.start(body.request, body.name, base, body.llm)


@router.get("/agent-drafts/{draft_id}")
@friendly
def get_draft(draft_id: str, state: WebState = Depends(web)):
    return state.builder.get(draft_id)


@router.delete("/agent-drafts/{draft_id}")
@friendly
def discard_draft(draft_id: str, state: WebState = Depends(web)):
    return state.builder.discard(draft_id)


@router.post("/agent-drafts/{draft_id}/chat", status_code=202)
@friendly
def draft_chat(draft_id: str, body: ChatBody, state: WebState = Depends(web)):
    return {"session_id": state.builder.chat(draft_id, body.text)}


@router.post("/agent-drafts/{draft_id}/llm")
@friendly
def draft_llm(draft_id: str, body: LlmBody, state: WebState = Depends(web)):
    return state.builder.set_llm(draft_id, body.provider, body.model)


@router.get("/agent-drafts/{draft_id}/file")
@friendly
def draft_file(draft_id: str, path: str, state: WebState = Depends(web)):
    return state.builder.read_file(draft_id, path)


@router.post("/agent-drafts/{draft_id}/stage")
@friendly
def stage_draft(draft_id: str, state: WebState = Depends(web)):
    return state.builder.stage(draft_id)


@router.post("/agents/{agent_id}/edit-draft")
@friendly
def edit_draft(agent_id: str, body: EditBody | None = None, state: WebState = Depends(web)):
    return state.builder.edit(check_id(agent_id, "agent id"), body.request if body else "")


@router.get("/agent-drafts/{draft_id}/icon.svg")
@friendly
def draft_icon(draft_id: str, state: WebState = Depends(web)):
    path = state.builder.icon_path(draft_id)
    if path is None:
        raise AgentError("This draft has no icon file", 404)
    return FileResponse(path, media_type="image/svg+xml")
