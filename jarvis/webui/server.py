"""FastAPI app factory and launcher for the Jarvis web UI."""

import os
import secrets
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import (routes_agents, routes_builder, routes_core, routes_discussions, routes_panels, routes_routines,
               routes_runtime, routes_tools, routes_voice)
from .security import COOKIE_NAME, SecurityMiddleware, allowed_hosts_for, is_loopback, token_matches
from .state import WebState

STATIC_DIR = Path(__file__).parent / "static"


def create_app(jarvis, token, host="127.0.0.1", port=7777):
    """Build the app around one Jarvis instance. All /api routes require `token`."""
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = WebState(jarvis, token)
    app.add_middleware(SecurityMiddleware, allowed_hosts=allowed_hosts_for(host, port))

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        supplied = request.query_params.get("token")
        if supplied is not None:
            if not token_matches(supplied, token):
                return PlainTextResponse("Invalid token", status_code=401)
            response = RedirectResponse("/", status_code=303)  # drops the token from the URL
            response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", path="/")
            return response
        return FileResponse(STATIC_DIR / "index.html")

    app.include_router(routes_core.router)
    app.include_router(routes_panels.router)
    app.include_router(routes_voice.router)
    app.include_router(routes_runtime.router)
    for feature in (routes_agents, routes_builder, routes_discussions, routes_routines, routes_tools):
        app.include_router(feature.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def check_bind(host, allow_remote):
    """Refuse non-loopback hosts unless explicitly allowed; returns a warning or ''."""
    if is_loopback(host):
        return ""
    if not allow_remote:
        raise ValueError(
            f"Refusing to bind to {host}: the agent can run shell commands. "
            "Use --allow-remote if you really want network access.")
    return (f"WARNING: listening on {host}. Anyone who can reach this port and has the "
            "token can run commands as you. The connection is plain HTTP.")


def run(jarvis, host="127.0.0.1", port=7777, open_browser=True, token=None, allow_remote=False):
    """Serve the web UI until interrupted."""
    import uvicorn

    warning = check_bind(host, allow_remote)
    token = token or os.environ.get("JARVIS_WEB_TOKEN") or secrets.token_urlsafe(32)
    app = create_app(jarvis, token, host=host, port=port)
    shown = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    url = f"http://{shown}:{port}/?token={token}"
    if warning:
        print(warning)
    print(f"Jarvis web UI: {url}")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.state.web.routines.start()  # the routine scheduler lives as long as the server
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        app.state.web.routines.stop()
        jarvis.close()
