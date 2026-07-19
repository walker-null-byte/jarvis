#!/usr/bin/env python3
"""Headless single-turn Jarvis for cron jobs."""

import sys
import os

# Ensure contextaware is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contextaware"))

from jarvis.config import load_config
from jarvis.agent import Jarvis


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

    # Run a single turn
    jarvis = Jarvis(config)
    reply = jarvis.ask(instruction)
    print(reply)


if __name__ == "__main__":
    main()