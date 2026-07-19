#!/usr/bin/env python3
"""Supervisor — relaunches Jarvis on crash and writes crash episodes."""

import os
import sys
import subprocess
import traceback
from pathlib import Path
from datetime import datetime

# Ensure local contextaware AND jarvis are on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "contextaware"))
sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.config import load_config
from jarvis.memory import EpisodeLog


def run_jarvis(args):
    """Run Jarvis with given args, return (exit_code, crash_info)."""
    cmd = [sys.executable, "-m", "jarvis"] + args
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "contextaware") + ":" + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()

    crash_info = None
    if proc.returncode != 0:
        crash_info = {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace")[-5000:],
            "stderr": stderr.decode(errors="replace")[-5000:],
            "timestamp": datetime.now().isoformat(),
            "command": " ".join(cmd),
        }

    return proc.returncode, crash_info


def log_crash(config, crash_info):
    """Write crash info to episodes."""
    episode = EpisodeLog(config["paths"]["mind"])
    episode.log("crash", f"Jarvis crashed:\n{json.dumps(crash_info, indent=2)}")

    # Also write a dedicated crash file for debugging
    crash_dir = Path(config["paths"]["mind"]) / "crashes"
    crash_dir.mkdir(exist_ok=True)
    crash_file = crash_dir / f"crash_{datetime.now():%Y%m%d_%H%M%S}.json"
    crash_file.write_text(json.dumps(crash_info, indent=2))


def main():
    config = load_config()
    max_restarts = config.get("supervisor", {}).get("max_restarts", 3)
    restart_delay = config.get("supervisor", {}).get("restart_delay", 5)

    restarts = 0
    while restarts < max_restarts:
        print(f"[supervisor] Starting Jarvis (attempt {restarts + 1}/{max_restarts})...", file=sys.stderr)
        exit_code, crash_info = run_jarvis(sys.argv[1:])

        if exit_code == 0:
            print("[supervisor] Jarvis exited cleanly", file=sys.stderr)
            break

        print(f"[supervisor] Jarvis crashed (exit {exit_code}), restarting in {restart_delay}s...", file=sys.stderr)
        log_crash(config, crash_info)
        restarts += 1

        if restarts < max_restarts:
            import time
            time.sleep(restart_delay)

    if restarts >= max_restarts:
        print(f"[supervisor] Max restarts ({max_restarts}) reached. Giving up.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    import json
    from datetime import datetime
    main()