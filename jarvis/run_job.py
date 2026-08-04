#!/usr/bin/env python3
"""Headless single-turn Jarvis for cron jobs."""

import sys
import datetime
from pathlib import Path

# Ensure jarvis is importable - use absolute path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.config import load_config
from jarvis.agent import Jarvis
from jarvis.jobs import set_job_status


def _update_status(job_name, started, result, success, config):
    return set_job_status(
        job_name,
        "succeeded" if success else "failed",
        result,
        config=config,
        started=started,
    )


def main():
    if len(sys.argv) < 2:
        print("Usage: run_job.py <job_name>")
        sys.exit(1)

    job_name = sys.argv[1]
    config = load_config()

    # Load the job instruction
    from jarvis.config import mind_dir
    job_path = mind_dir(config) / "jobs" / f"{job_name}.md"

    if not job_path.exists():
        print(f"Job '{job_name}' not found at {job_path}")
        sys.exit(1)

    content = job_path.read_text()
    # Extract instruction from the job file
    lines = content.splitlines()
    instruction = ""
    in_instruction = False
    for line in lines:
        if line.strip() == "## Instruction":
            in_instruction = True
            continue
        if line.startswith("## ") and in_instruction:
            break
        if in_instruction:
            instruction += line + "\n"

    instruction = instruction.strip()
    if not instruction:
        print(f"No instruction found in job '{job_name}'")
        sys.exit(1)

    # Run a single turn and persist an observable result for the job panel.
    started = datetime.datetime.now().isoformat(timespec="seconds")
    set_job_status(job_name, "running", config=config, started=started)
    jarvis = None
    try:
        jarvis = Jarvis(config)
        reply = jarvis.ask(instruction)
        print(reply)
        _update_status(job_name, started, reply, True, config)
    except Exception as e:
        _update_status(job_name, started, str(e), False, config)
        print(f"Job failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if jarvis is not None:
            jarvis.close()
            jarvis.registry.stop_watcher()


if __name__ == "__main__":
    main()
