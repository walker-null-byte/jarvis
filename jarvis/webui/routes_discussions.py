"""Discussions routes: list/create/edit, controls, user messages, Markdown export."""

import re

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .state import WebState, check_id, fail, web

router = APIRouter(prefix="/api")


class SpecBody(BaseModel):
    model_config = {"extra": "allow"}  # the spec is validated by jarvis.discussions.validate_spec


class TextBody(BaseModel):
    text: str = ""


class ItemBody(BaseModel):
    title: str = ""
    notes: str = ""


def _manager(state):
    return state.discussions


def _known(state, discussion_id):
    check_id(discussion_id, "discussion id")
    if _manager(state).get(discussion_id) is None:
        fail(404, "Discussion not found")
    return _manager(state)


@router.get("/discussions")
def list_discussions(state: WebState = Depends(web)):
    return {"discussions": _manager(state).list()}


@router.get("/discussions/meta")
def discussions_meta(state: WebState = Depends(web)):
    return _manager(state).meta()


@router.post("/discussions", status_code=201)
def create_discussion(body: SpecBody, state: WebState = Depends(web)):
    try:
        discussion, warnings = _manager(state).create(body.model_dump())
    except ValueError as error:
        fail(400, str(error))
    return {"discussion": discussion, "warnings": warnings}


@router.get("/discussions/{discussion_id}")
def get_discussion(discussion_id: str, state: WebState = Depends(web)):
    manager = _known(state, discussion_id)
    return manager.detail(discussion_id)


@router.patch("/discussions/{discussion_id}")
def patch_discussion(discussion_id: str, body: SpecBody, state: WebState = Depends(web)):
    manager = _known(state, discussion_id)
    try:
        discussion, warnings = manager.patch(discussion_id, body.model_dump())
    except ValueError as error:
        fail(400, str(error))
    return {"discussion": discussion, "warnings": warnings}


@router.delete("/discussions/{discussion_id}")
def delete_discussion(discussion_id: str, state: WebState = Depends(web)):
    _known(state, discussion_id).delete(discussion_id)
    return {"deleted": True}


@router.post("/discussions/{discussion_id}/items")
def add_item(discussion_id: str, body: ItemBody, state: WebState = Depends(web)):
    manager = _known(state, discussion_id)
    try:
        return {"discussion": manager.add_item(discussion_id, body.title, body.notes)}
    except ValueError as error:
        fail(400, str(error))


@router.post("/discussions/{discussion_id}/message", status_code=202)
def post_message(discussion_id: str, body: TextBody, state: WebState = Depends(web)):
    manager = _known(state, discussion_id)
    try:
        return {"message": manager.message(discussion_id, body.text)}
    except ValueError as error:
        fail(409 if "finished" in str(error) else 400, str(error))


@router.post("/discussions/{discussion_id}/{action}")
def control_discussion(discussion_id: str, action: str, state: WebState = Depends(web)):
    if action not in ("start", "pause", "resume", "next", "conclude", "stop"):
        fail(404, "Unknown action")
    manager = _known(state, discussion_id)
    try:
        return {"discussion": manager.control(discussion_id, action)}
    except ValueError as error:
        fail(409, str(error))


@router.get("/discussions/{discussion_id}/export.md")
def export_discussion(discussion_id: str, state: WebState = Depends(web)):
    manager = _known(state, discussion_id)
    title = manager.get(discussion_id)["title"]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-")[:60] or "discussion"
    return PlainTextResponse(manager.export_markdown(discussion_id), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}.md"'})
