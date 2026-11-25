"""Tools routes: MCP servers (list, add, enable/trust, restart, remove)."""

from fastapi import APIRouter, Body, Depends

from .. import mcp_client
from .state import WebState, check_id, fail, web

router = APIRouter(prefix="/api")


def _manager(state):
    jarvis = state.jarvis
    return mcp_client.manager_for(jarvis.registry, jarvis.mind, jarvis.events)


@router.get("/mcp/servers")
def list_servers(state: WebState = Depends(web)):
    manager = _manager(state)
    return {"servers": manager.list(), "file": str(manager.path)}


@router.post("/mcp/servers")
def add_servers(payload=Body(...), state: WebState = Depends(web)):
    """Body: a server object with a "name", {"name": {...}}, {"mcpServers": {...}}, or {"json": "<pasted text>"}."""
    if isinstance(payload, dict) and set(payload) == {"json"}:
        payload = payload["json"]
    manager = _manager(state)
    try:
        added = manager.add(payload)
    except ValueError as error:
        fail(400, str(error))
    return {"added": added, "servers": manager.list()}


def _known(manager, name):
    check_id(name, "server name")
    if name not in manager.servers:
        fail(404, f"No MCP server named {name}")


@router.patch("/mcp/servers/{name}")
def update_server(name: str, payload: dict = Body(...), state: WebState = Depends(web)):
    manager = _manager(state)
    _known(manager, name)
    manager.update(name, enabled=payload.get("enabled"), trust=payload.get("trust"))
    return {"servers": manager.list()}


@router.post("/mcp/servers/{name}/restart")
def restart_server(name: str, state: WebState = Depends(web)):
    manager = _manager(state)
    _known(manager, name)
    manager.restart(name)
    return {"servers": manager.list()}


@router.delete("/mcp/servers/{name}")
def remove_server(name: str, state: WebState = Depends(web)):
    manager = _manager(state)
    _known(manager, name)
    manager.remove(name)
    return {"servers": manager.list()}
