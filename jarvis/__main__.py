"""Entry point: `python -m jarvis` for the TUI, `python -m jarvis --cli` for plain mode."""

import argparse
import json
import sys

from .agent import Jarvis
from .config import load_config


def cli_loop(jarvis):
    print("JARVIS online. (exit with Ctrl-D or 'exit')\n")
    
    # Initialize voice for CLI
    from jarvis.voice import get_voice
    voice = get_voice(jarvis.config)
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
