"""Agents routes: list/import/install agent plugins, their settings, records, actions and exports.

All the work happens in `state.agents` (jarvis/specialists/manager.py); these
handlers only translate HTTP. Errors come back as {"error": message, ...extra}
(extra: `fields` for per-field problems, `issues` for manifest problems).
"""

import functools
import re

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from ..specialists import AgentError, ImportProblem, ManifestError
from ..specialists.manifest import format_issue
from ..specialists.packaging import MAX_BYTES
from .state import WebState, check_id, web

router = APIRouter(prefix="/api")


def _error(error):
    if isinstance(error, AgentError):
        return JSONResponse({"error": str(error), **error.extra}, status_code=error.status)
    if isinstance(error, ManifestError):
        issues = [format_issue(i) for i in error.issues]
        return JSONResponse({"error": "The agent's files have problems:\n" + "\n".join(issues), "issues": issues},
                            status_code=400)
    if isinstance(error, ImportProblem):
        return JSONResponse({"error": f"Can't import: {error}"}, status_code=400)
    raise error


def friendly(fn):
    """Turn agent errors into JSON replies with a helpful message."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (AgentError, ManifestError, ImportProblem) as error:
            return _error(error)
    return wrapper


def _download(filename, media_type, body):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename) or "download"
    charset = "; charset=utf-8" if media_type.startswith("text/") or media_type.endswith("json") else ""
    return Response(body, media_type=media_type + charset,
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


class CreateBody(BaseModel):
    name: str = ""
    description: str = ""
    prompt: str = ""
    icon: str = "agents"


class InstallBody(BaseModel):
    staging: str
    mode: str = "new"
    trust: bool = False


class TrustBody(BaseModel):
    trusted: bool = True


class SettingsBody(BaseModel):
    values: dict = {}
    secrets: dict = {}
    llm: dict | None = None


class ActionBody(BaseModel):
    inputs: dict = {}
    session_id: str | None = None


class AgentChatBody(BaseModel):
    text: str
    session_id: str | None = None


class RecordBody(BaseModel):
    fields: dict


# ---------- agents ----------

@router.get("/agents")
@friendly
def list_agents(state: WebState = Depends(web)):
    return {"agents": state.agents.list(), "broken": state.agents.broken()}


@router.post("/agents")
@friendly
def create_agent(body: CreateBody, state: WebState = Depends(web)):
    return state.agents.create(body.name, body.description, body.prompt, body.icon)


@router.post("/agents/import")
async def import_agent(request: Request, state: WebState = Depends(web)):
    """A .zip upload (multipart field "file"), or JSON {url} (GitHub / https zip) or {path} (local folder)."""
    manager = state.agents
    try:
        kind = request.headers.get("content-type", "")
        if kind.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise AgentError("Choose a .zip file to import")
            data = await upload.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ImportProblem(f"the file is larger than {MAX_BYTES // 1_000_000} MB")
            return await run_in_threadpool(manager.import_zip, data, getattr(upload, "filename", "") or "upload")
        try:
            body = await request.json()
        except ValueError:
            raise AgentError("Send a .zip file, or JSON with a url or a folder path") from None
        if not isinstance(body, dict):
            raise AgentError("Send JSON like {\"url\": \"https://github.com/…\"}")
        if str(body.get("url") or "").strip():
            return await run_in_threadpool(manager.import_url, str(body["url"]))
        if str(body.get("path") or "").strip():
            return await run_in_threadpool(manager.import_folder, str(body["path"]).strip())
        raise AgentError("Paste a GitHub link, choose a .zip file, or give a folder path")
    except (AgentError, ManifestError, ImportProblem) as error:
        return _error(error)


@router.delete("/agents/import/{staging}")
def discard_import(staging: str, state: WebState = Depends(web)):
    state.agents.discard(staging)
    return {"discarded": True}


@router.get("/agents/{agent_id}")
@friendly
def get_agent(agent_id: str, state: WebState = Depends(web)):
    return state.agents.detail(check_id(agent_id, "agent id"))


@router.get("/agents/{agent_id}/icon.svg")
@friendly
def agent_icon(agent_id: str, state: WebState = Depends(web)):
    path = state.agents.icon_path(check_id(agent_id, "agent id"))
    if path is None:
        raise AgentError("This agent has no icon file", 404)
    return FileResponse(path, media_type="image/svg+xml")


@router.post("/agents/{agent_id}/install")
@friendly
def install_agent(agent_id: str, body: InstallBody, state: WebState = Depends(web)):
    return state.agents.install(body.staging, body.mode, body.trust)


@router.post("/agents/{agent_id}/trust")
@friendly
def trust_agent(agent_id: str, body: TrustBody, state: WebState = Depends(web)):
    return state.agents.trust(check_id(agent_id, "agent id"), body.trusted)


@router.delete("/agents/{agent_id}")
@friendly
def remove_agent(agent_id: str, data: bool = True, state: WebState = Depends(web)):
    return state.agents.remove(check_id(agent_id, "agent id"), delete_data=data)


@router.post("/agents/{agent_id}/duplicate")
@friendly
def duplicate_agent(agent_id: str, state: WebState = Depends(web)):
    return state.agents.duplicate(check_id(agent_id, "agent id"))


@router.get("/agents/{agent_id}/export.zip")
@friendly
def export_agent(agent_id: str, data: bool = False, state: WebState = Depends(web)):
    filename, body = state.agents.export_zip(check_id(agent_id, "agent id"), with_data=data)
    return _download(filename, "application/zip", body)


# ---------- settings ----------

@router.get("/agents/{agent_id}/settings")
@friendly
def get_settings(agent_id: str, state: WebState = Depends(web)):
    return state.agents.settings_info(check_id(agent_id, "agent id"))


@router.put("/agents/{agent_id}/settings")
@friendly
def put_settings(agent_id: str, body: SettingsBody, state: WebState = Depends(web)):
    return state.agents.save_settings(check_id(agent_id, "agent id"), body.values, body.secrets, body.llm)


# ---------- records ----------

@router.get("/agents/{agent_id}/records/{table}")
@friendly
def list_records(agent_id: str, table: str, q: str = "", state: WebState = Depends(web)):
    records = state.agents.table(check_id(agent_id, "agent id"), table)
    result = records.query(search=q, limit=0)
    return {"table": records.spec, **result}


@router.patch("/agents/{agent_id}/records/{table}/{record_id}")
@friendly
def update_record(agent_id: str, table: str, record_id: str, body: RecordBody, state: WebState = Depends(web)):
    from ..specialists.records import FieldError
    records = state.agents.table(check_id(agent_id, "agent id"), table)
    try:
        return records.update(record_id, body.fields)
    except KeyError:
        raise AgentError("That record no longer exists", 404) from None
    except FieldError as error:
        raise AgentError(str(error)) from None


@router.delete("/agents/{agent_id}/records/{table}/{record_id}")
@friendly
def delete_record(agent_id: str, table: str, record_id: str, state: WebState = Depends(web)):
    if not state.agents.table(check_id(agent_id, "agent id"), table).delete(record_id):
        raise AgentError("That record no longer exists", 404)
    return {"deleted": record_id}


# ---------- actions, chat, exports ----------

@router.post("/agents/{agent_id}/actions/{action_id}", status_code=202)
@friendly
def run_action(agent_id: str, action_id: str, body: ActionBody, state: WebState = Depends(web)):
    session_id = body.session_id and check_id(body.session_id, "session id")
    sid = state.agents.run_action(check_id(agent_id, "agent id"), action_id, body.inputs, session_id)
    return {"session_id": sid}


@router.post("/agents/{agent_id}/actions/{action_id}/preview")
@friendly
def preview_action(agent_id: str, action_id: str, body: ActionBody, state: WebState = Depends(web)):
    return {"prompt": state.agents.render_action(check_id(agent_id, "agent id"), action_id, body.inputs)}


@router.post("/agents/{agent_id}/chat", status_code=202)
@friendly
def agent_chat(agent_id: str, body: AgentChatBody, state: WebState = Depends(web)):
    session_id = body.session_id and check_id(body.session_id, "session id")
    return {"session_id": state.agents.chat(check_id(agent_id, "agent id"), body.text, session_id)}


@router.post("/agents/{agent_id}/sessions")
@friendly
def new_agent_session(agent_id: str, state: WebState = Depends(web)):
    return {"session_id": state.agents.new_session(check_id(agent_id, "agent id"))}


@router.get("/agents/{agent_id}/exports/{export_id}")
@friendly
def run_export(agent_id: str, export_id: str, state: WebState = Depends(web)):
    filename, media, body = state.agents.export(check_id(agent_id, "agent id"), export_id)
    return _download(filename, media, body)
