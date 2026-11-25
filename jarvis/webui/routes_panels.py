"""Drawer-panel routes: characters, mind, memory, tools, jobs, providers, settings."""

import base64
import binascii
import shlex

import json

from fastapi import APIRouter, Body, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .state import WebState, check_id, fail, web

router = APIRouter(prefix="/api")

MAX_MIND_BYTES = 64 * 1024
MAX_IMPORT_BYTES = 10 * 1024 * 1024
AVATAR_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MIND_FILES = {  # name -> editable
    "user.md": True, "system.md": True, "self.md": True,
    "MEMORY.md": False,
}


def _bad_result(result, status=400):
    """Turn an {"error": ...} result dict from the core modules into an HTTP error."""
    if isinstance(result, dict) and result.get("error"):
        fail(status, result["error"])
    return result


# ---------- characters (module written by another workstream; imported lazily) ----------

def _char_store(state):
    store = getattr(state.jarvis, "store_characters", None)
    if store is not None:
        return store
    try:
        from ..characters import CharacterStore
    except ImportError:
        fail(503, "Character cards are not available in this build")
    state.jarvis.store_characters = CharacterStore(state.jarvis.mind)
    return state.jarvis.store_characters


class ImportBody(BaseModel):
    filename: str
    data_base64: str


@router.get("/characters")
def list_characters(state: WebState = Depends(web)):
    active = getattr(state.jarvis, "character", "")
    return {"characters": _char_store(state).list(), "active": active}


@router.post("/characters/import")
def import_character(body: ImportBody, state: WebState = Depends(web)):
    store = _char_store(state)
    if len(body.data_base64) > MAX_IMPORT_BYTES * 4 // 3 + 16:
        fail(413, "File too large")
    try:
        data = base64.b64decode(body.data_base64, validate=True)
        return store.import_bytes(data, body.filename)
    except (binascii.Error, ValueError) as error:
        fail(400, str(error) or "Could not import this file")


def _card_fields(payload):
    """Accept {"fields": {...}} or the fields directly; strings and lists of strings only."""
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
    for key, value in fields.items():
        ok = isinstance(value, str) or (
            isinstance(value, list) and all(isinstance(item, str) for item in value))
        if not ok:
            fail(400, f"Field {key!r} must be a string or a list of strings")
    return fields


def _saved(state, store, card_id, run):
    try:
        summary = run()
    except ValueError as error:
        fail(400, str(error) or "Invalid character")
    return {"character": summary, "card": store.get(summary["id"] if summary else card_id)}


@router.post("/characters")
def create_character(payload: dict = Body(...), state: WebState = Depends(web)):
    store = _char_store(state)
    fields = _card_fields(payload)
    return _saved(state, store, None, lambda: store.save(fields))


@router.put("/characters/{card_id}")
def update_character(card_id: str, payload: dict = Body(...), state: WebState = Depends(web)):
    check_id(card_id, "character id")
    store = _char_store(state)
    if store.get(card_id) is None:
        fail(404, "Character not found")
    fields = _card_fields(payload)
    return _saved(state, store, card_id, lambda: store.save(fields, card_id))


@router.post("/characters/{card_id}/reset")
def reset_character(card_id: str, state: WebState = Depends(web)):
    check_id(card_id, "character id")
    store = _char_store(state)
    if not store.reset(card_id):
        fail(404, "Nothing to reset (not a customized built-in card)")
    return {"reset": card_id, "card": store.get(card_id)}


@router.get("/characters/{card_id}/export")
def export_character(card_id: str, state: WebState = Depends(web)):
    check_id(card_id, "character id")
    store = _char_store(state)
    if store.get(card_id) is None:
        fail(404, "Character not found")
    data = json.dumps(store.export(card_id), ensure_ascii=False, indent=2)
    return Response(data, media_type="application/json; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{card_id}.json"'})


@router.get("/characters/{card_id}")
def get_character(card_id: str, state: WebState = Depends(web)):
    card = _char_store(state).get(check_id(card_id, "character id"))
    if card is None:
        fail(404, "Character not found")
    return card


@router.post("/characters/{card_id}/activate")
def activate_character(card_id: str, state: WebState = Depends(web)):
    check_id(card_id, "character id")
    if not hasattr(state.jarvis, "set_character"):
        fail(501, "Switching characters is not supported by this agent build")
    if state.busy:
        fail(409, "Finish or cancel the running turn first")
    _bad_result(state.jarvis.set_character(card_id), 404)
    return state.snapshot()


@router.delete("/characters/{card_id}")
def delete_character(card_id: str, state: WebState = Depends(web)):
    check_id(card_id, "character id")
    if not _char_store(state).delete(card_id):
        fail(404, "Not found, or a built-in card that cannot be deleted")
    return {"deleted": card_id}


@router.get("/characters/{card_id}/avatar")
def character_avatar(card_id: str, state: WebState = Depends(web)):
    path = _char_store(state).avatar_path(check_id(card_id, "character id"))
    if not path or path.suffix.lower() not in AVATAR_SUFFIXES or not path.is_file():
        fail(404, "No avatar")
    return FileResponse(path)


# ---------- mind files ----------

class ContentBody(BaseModel):
    content: str


def _mind_path(state, name):
    if name not in MIND_FILES:  # whitelist: also rules out any path traversal
        fail(404, "Unknown mind file")
    return state.jarvis.mind / name


@router.get("/mind")
def list_mind(state: WebState = Depends(web)):
    files = []
    for name, editable in MIND_FILES.items():
        path = state.jarvis.mind / name
        files.append({"name": name, "editable": editable,
                      "size": path.stat().st_size if path.exists() else 0})
    return {"files": files}


@router.get("/mind/{name}")
def read_mind(name: str, state: WebState = Depends(web)):
    path = _mind_path(state, name)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"name": name, "editable": MIND_FILES[name], "content": text}


@router.put("/mind/{name}")
def write_mind(name: str, body: ContentBody, state: WebState = Depends(web)):
    path = _mind_path(state, name)
    if not MIND_FILES[name]:
        fail(403, f"{name} is read-only")
    if len(body.content.encode("utf-8")) > MAX_MIND_BYTES:
        fail(413, "File too large (64 KB max)")
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(body.content, encoding="utf-8")
    temporary.replace(path)
    return {"name": name, "saved": True}


def _run_maintenance(state, function):
    """Run a slow job (dream cycle, codify) if no other one is running."""
    if not state.maintenance_lock.acquire(blocking=False):
        fail(409, "Another maintenance job is running")
    try:
        return function()
    except Exception as error:
        fail(500, f"{type(error).__name__}: {error}")
    finally:
        state.maintenance_lock.release()


@router.post("/mind/consolidate")
def consolidate_mind(state: WebState = Depends(web)):
    from ..consolidate import consolidate
    jarvis = state.jarvis
    return _run_maintenance(state, lambda: consolidate(mind=jarvis.mind, config=jarvis.config))


# ---------- memory ----------

class ReviewBody(BaseModel):
    decision: str
    note: str = ""


def _facts(state):
    from ..memory import FactStore
    return FactStore(state.jarvis.mind)


@router.get("/memory")
def list_memory(state: WebState = Depends(web)):
    return _facts(state).review_memories(limit=0, include_approved=True)


@router.get("/memory/recall")
def recall_memory(q: str = "", state: WebState = Depends(web)):
    result = _facts(state).recall(q, top_n=8)
    if isinstance(result, dict):
        return {"results": [], "note": result.get("note", "")}
    return {"results": result}


@router.post("/memory/{name}/review")
def review_memory(name: str, body: ReviewBody, state: WebState = Depends(web)):
    return _bad_result(_facts(state).set_review(check_id(name, "memory name"), body.decision, body.note))


@router.delete("/memory/{name}")
def forget_memory(name: str, state: WebState = Depends(web)):
    return _bad_result(_facts(state).forget(check_id(name, "memory name")), 404)


# ---------- tools & skills ----------

@router.get("/tools")
def list_tools(state: WebState = Depends(web)):
    exposure = getattr(state.jarvis, "exposure", None)
    core = set(exposure.core_names()) if exposure else set(
        state.jarvis.config.get("agent", {}).get("core_tools", []))
    tools = state.jarvis.registry.list_tools(with_summary=True)
    return {"tools": [{**tool, "core": tool["name"] in core} for tool in tools]}


@router.get("/tools/search")
def search_tools(q: str = "", state: WebState = Depends(web)):
    if not q.strip():
        return {"query": q, "tools": []}
    return state.jarvis.registry.search_tools(q, top_n=8)


@router.get("/skills")
def list_skills(state: WebState = Depends(web)):
    skills = []
    for path in sorted((state.jarvis.mind / "skills").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
        skills.append({"name": path.stem, "title": title, "content": text[:MAX_MIND_BYTES]})
    return {"skills": skills}


@router.post("/skills/codify")
def codify(state: WebState = Depends(web)):
    from ..codify import codify_skills
    jarvis = state.jarvis
    return _run_maintenance(state, lambda: codify_skills(mind=jarvis.mind, config=jarvis.config))


# ---------- jobs ----------

def _job_name(name):
    return check_id(name, "job name")


@router.get("/jobs")
def list_jobs(state: WebState = Depends(web)):
    from ..jobs import cron_list
    return cron_list(state.jarvis.config)


@router.post("/jobs/{name}/enable")
def enable_job(name: str, state: WebState = Depends(web)):
    from ..jobs import cron_enable
    return _bad_result(cron_enable(_job_name(name), state.jarvis.config), 404)


@router.post("/jobs/{name}/disable")
def disable_job(name: str, state: WebState = Depends(web)):
    from ..jobs import cron_disable
    return _bad_result(cron_disable(_job_name(name), state.jarvis.config), 404)


@router.delete("/jobs/{name}")
def delete_job(name: str, state: WebState = Depends(web)):
    from ..jobs import cron_remove
    return _bad_result(cron_remove(_job_name(name), state.jarvis.config), 404)


# ---------- providers & settings (go through the slash-command layer) ----------

class ProviderBody(BaseModel):
    provider: str
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""


class TestBody(BaseModel):
    provider: str = ""
    base_url: str = ""
    api_key_env: str = ""


# Settings the slash-command layer does not expose; validated here.
EXTRA_SETTINGS = {"agent.tool_mode": ("search", "all")}


class SettingBody(BaseModel):
    path: str
    value: object


def _command(state, *parts):
    from ..commands import handle_slash_command
    output = handle_slash_command(state.jarvis, " ".join(shlex.quote(str(p)) for p in parts))
    return output


def _presets():
    from ..commands import PROVIDER_PRESETS
    return PROVIDER_PRESETS


@router.get("/providers")
def list_providers(state: WebState = Depends(web)):
    active = state.jarvis.config["llm"].get("provider", "custom")
    ordered = sorted(_presets().items(), key=lambda pair: pair[1].get("order", 100))
    providers = [{"id": name, "label": preset.get("label", name),
                  "kind": preset.get("kind", "api_key"),
                  "active": name == active,
                  "base_url": preset.get("base_url", ""),
                  "api_key_env": preset.get("api_key_env", ""),
                  "description": preset.get("description", ""),
                  "backend": preset.get("backend", "chat")}
                 for name, preset in ordered]
    providers.append({"id": "custom", "label": "Custom — any OpenAI-compatible endpoint",
                      "kind": "api_key", "active": active == "custom", "base_url": "",
                      "api_key_env": "", "backend": "chat",
                      "description": "Any reachable OpenAI-compatible endpoint"})
    return {"providers": providers, "active": active}


@router.post("/provider")
def set_provider(body: ProviderBody, state: WebState = Depends(web)):
    if state.busy:
        fail(409, "Finish or cancel the running turn first")
    provider = body.provider.strip().lower()
    if provider in _presets():
        outputs = [_command(state, "/provider", "use", provider)]
        if body.api_key_env.strip():
            outputs.append(_apply_preset_credential(state, body.api_key_env.strip()))
    elif provider == "custom":
        if not body.base_url.strip():
            fail(400, "A custom provider needs a base URL")
        extra = [body.api_key_env.strip()] if body.api_key_env.strip() else []
        outputs = [_command(state, "/provider", "set", body.base_url.strip(), *extra)]
    else:
        fail(400, f"Unknown provider: {provider}")
    if body.model.strip():
        outputs.append(_command(state, "/model", body.model.strip()))
    return {"output": "\n".join(outputs), "status": state.status()}


@router.post("/provider/test")
def test_provider(body: TestBody, state: WebState = Depends(web)):
    from ..commands import _test_connection
    presets = _presets()
    base_url, key_env = body.base_url.strip() or None, body.api_key_env.strip() or None
    preset = presets.get(body.provider.strip().lower())
    if preset and not base_url:
        base_url, key_env = preset["base_url"], key_env or preset["api_key_env"]
    ok, message, models, working_url = _test_connection(
        state.jarvis, base_url=base_url, api_key_env=key_env, check_model=False,
        provider=body.provider.strip().lower() or None)
    return {"ok": ok, "message": message, "models": models, "base_url": working_url}


def _apply_preset_credential(state, raw):
    """Use a typed env var name or literal key with a preset (keeps its backend).

    An env var name is persisted like any provider setting; a literal key stays
    in memory for this run only and is never written to disk.
    """
    from ..commands import _split_credentials, _test_connection
    from ..config import save_runtime_overrides
    llm = state.jarvis.config["llm"]
    env_name, literal = _split_credentials(raw)
    if env_name:
        llm["api_key_env"], llm["api_key"] = env_name, None
        save_runtime_overrides(state.jarvis.config, ["llm.api_key_env"])
    else:
        llm["api_key"] = literal
    state.jarvis.llm.reconfigure(state.jarvis.config)
    state.jarvis._emit("config_changed", section="llm", key="api_key")
    ok, message, _, _ = _test_connection(state.jarvis, check_model=False)
    source = f"env var {env_name}" if env_name else "a key for this session only"
    return f"Credentials: using {source}. Connection test: {'ok' if ok else 'failed — ' + message}"


@router.get("/models")
def list_models(state: WebState = Depends(web)):
    from ..commands import _test_connection
    try:
        ok, message, models, _ = _test_connection(state.jarvis, check_model=False)
    except Exception as error:  # e.g. server down, not signed in: not a server fault
        ok, message, models = False, str(error), []
    return {"ok": ok, "message": message, "models": models,
            "current": state.jarvis.config["llm"].get("model", "")}


@router.get("/settings")
def get_settings(state: WebState = Depends(web)):
    from ..commands import SETTING_PATHS
    config = state.jarvis.config
    values = {}
    for path in SETTING_PATHS:
        section, key = path.split(".", 1)
        values[path] = config.get(section, {}).get(key)
    for path in EXTRA_SETTINGS:
        section, key = path.split(".", 1)
        values[path] = config.get(section, {}).get(key)
    return {"settings": values}


@router.post("/settings")
def set_setting(body: SettingBody, state: WebState = Depends(web)):
    from ..commands import SETTING_PATHS
    if body.path in EXTRA_SETTINGS:
        if body.value not in EXTRA_SETTINGS[body.path]:
            fail(400, f"{body.path} must be one of: {', '.join(EXTRA_SETTINGS[body.path])}")
        section, key = body.path.split(".", 1)
        state.jarvis.config[section][key] = body.value
        state.emit("config_changed", section=section, key=body.path)
        return {"output": f"Set {body.path} = {body.value!r}", "status": state.status()}
    if body.path not in SETTING_PATHS:
        fail(400, f"Unsupported setting: {body.path}")
    value = ("on" if body.value else "off") if isinstance(body.value, bool) else str(body.value)
    if "\n" in value:
        fail(400, "Setting values must be a single line")
    output = _command(state, "/settings", "set", body.path, value)
    return {"output": output, "status": state.status()}


@router.post("/settings/save")
def save_settings(state: WebState = Depends(web)):
    return {"output": _command(state, "/settings", "save")}


# ---------- ChatGPT sign-in (module written by another workstream; imported lazily) ----------

def _chatgpt(state):
    try:
        from ..providers.chatgpt_auth import get_chatgpt_auth
    except ImportError:
        fail(501, "Sign in with ChatGPT is not available in this build")
    return get_chatgpt_auth(state.jarvis.config)


@router.get("/auth/chatgpt")
def chatgpt_status(state: WebState = Depends(web)):
    return _chatgpt(state).status()


@router.post("/auth/chatgpt/login")
def chatgpt_login(state: WebState = Depends(web)):
    try:
        return _chatgpt(state).start_login(open_browser=False)
    except RuntimeError as error:
        fail(502, str(error))


@router.post("/auth/chatgpt/logout")
def chatgpt_logout(state: WebState = Depends(web)):
    return _chatgpt(state).logout()


# ---------- onboarding ----------

@router.get("/onboarding")
def get_onboarding(state: WebState = Depends(web)):
    llm = state.jarvis.config["llm"]
    return {"done": state.onboarded, "provider": llm.get("provider", "custom"),
            "model": llm.get("model", ""), "chatgpt": state.chatgpt_status()}


@router.post("/onboarding/done")
def finish_onboarding(state: WebState = Depends(web)):
    state.set_ui_state(onboarded=True)
    return {"done": True}
