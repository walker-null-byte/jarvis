"""Entry point: `python -m jarvis` for the TUI, `python -m jarvis --cli` for plain mode."""

import argparse
import json
import sys

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

        command_result = handle_slash_command(jarvis, text)
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


def main():
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--cli", action="store_true", help="plain terminal mode (no TUI)")
    args = parser.parse_args()

    jarvis = Jarvis(load_config())

    if args.cli:
        cli_loop(jarvis)
    else:
        from .ui import run_ui
        run_ui(jarvis)


if __name__ == "__main__":
    main()
