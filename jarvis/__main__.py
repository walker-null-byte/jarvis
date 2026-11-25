"""Entry point: `python -m jarvis` for the TUI, `python -m jarvis --cli` for plain mode."""

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import Jarvis
from .commands import handle_slash_command
from .config import load_config


def cli_loop(jarvis):
    print("JARVIS online. (exit with Ctrl-D or 'exit')\n")
    
    # Keep this reference current because /settings can replace the voice
    # engine while the CLI is running.
    voice = jarvis.voice

    def approve(action, payload):
        print(f"\n  ⚠ Approval required for {action}: {json.dumps(payload, default=str)[:240]}")
        try:
            return input("  Allow? [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    jarvis.approvals.set_fallback(approve)
    voice.set_amplitude_callback(lambda a: None)  # silent amplitude in CLI
    
    while True:
        try:
            text = input("❯ ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break

        command_result = handle_slash_command(jarvis, text, blocking=True)
        if command_result is not None:
            print(f"\n{command_result}\n")
            voice = jarvis.voice
            voice.set_amplitude_callback(lambda a: None)
            continue

        def on_text(token):
            print(token, end="", flush=True)

        def on_tool(name, args):
            print(f"\n  ⚙ {name} {json.dumps(args, default=str)[:120]}", flush=True)

        try:
            reply = jarvis.ask(text, on_text=on_text, on_tool=on_tool)
            if reply and not sys.stdout.isatty():
                pass  # already streamed
            print("\n")
            # Speak the reply
            voice.speak(reply)
        except Exception as e:
            print(f"\n✗ {e}\n", file=sys.stderr)


def run_web(args):
    """Start the local web UI (`python -m jarvis --web`)."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("The web UI packages are missing; reinstall Jarvis: pip install -e .", file=sys.stderr)
        sys.exit(1)
    from .webui.server import check_bind, run

    config = load_config()
    web_cfg = config.get("webui", {})
    host = args.host or web_cfg.get("host", "127.0.0.1")
    port = args.port or int(web_cfg.get("port", 7777))
    open_browser = web_cfg.get("open_browser", True) and not args.no_browser
    try:
        check_bind(host, args.allow_remote)
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(2)
    run(Jarvis(config), host, port, open_browser, allow_remote=args.allow_remote)


def main():
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis — local-first terminal AI assistant")
    parser.add_argument("--cli", action="store_true", help="plain terminal mode (no TUI)")
    parser.add_argument("--doctor", action="store_true", help="check endpoint, config, and mind health")
    parser.add_argument("--version", action="store_true", help="show version")
    parser.add_argument("--web", action="store_true", help="serve the local web UI")
    parser.add_argument("--host", help="web UI host (default: config webui.host, 127.0.0.1)")
    parser.add_argument("--port", type=int, help="web UI port (default: config webui.port, 7777)")
    parser.add_argument("--no-browser", action="store_true", help="web UI: do not open a browser")
    parser.add_argument("--allow-remote", action="store_true",
                        help="web UI: allow binding to a non-loopback address (dangerous)")
    args = parser.parse_args()

    if args.version:
        try:
            from importlib.metadata import version
            print(f"jarvis {version('jarvis')}")
        except Exception:
            print("jarvis 0.1.0")
        return

    if args.doctor:
        cfg = load_config()
        llm = cfg.get("llm", {})
        print(f"Config: {cfg.get('paths', {}).get('mind', 'mind')}  provider={llm.get('provider')}  model={llm.get('model')}")
        print(f"Endpoint: {llm.get('base_url')}")
        api_key = llm.get("api_key") or os.environ.get(llm.get("api_key_env") or "") or ""
        print(f"API key: {'set (' + llm.get('api_key_env','') + ')' if api_key else 'not set'}  env={llm.get('api_key_env')}")
        mind = cfg.get("paths", {}).get("mind", "mind")
        p = Path(mind)
        print(f"Mind dir: {p.resolve()}  exists={p.exists()}")
        try:
            from .llm import LLM
            probe = LLM(cfg)
            ok, msg, ids, _ = probe.probe(timeout=5)
            print(f"Probe: {'OK' if ok else 'FAILED'} — {msg}")
            if ids:
                print(f"Models: {', '.join(ids[:10])}")
        except Exception as e:
            print(f"Probe error: {e}")
        return

    if args.web:
        run_web(args)
        return

    jarvis = Jarvis(load_config())

    if args.cli:
        cli_loop(jarvis)
    else:
        from .ui import run_ui
        run_ui(jarvis)


if __name__ == "__main__":
    main()
