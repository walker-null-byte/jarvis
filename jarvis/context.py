"""The mind/ folder and the system prompt built from it.

First run creates the scaffolding; every turn the prompt is rebuilt from the
files, so anything Jarvis (or the user) writes into mind/ takes effect on the
next message.
"""

import datetime
import os
import platform
import re
from pathlib import Path

from .characters import CharacterStore, render_persona
from .config import mind_dir

SELF_MD = """\
# Who you are
You are Jarvis, a personal AI agent running locally on your user's computer,
inspired by the JARVIS of Iron Man. You are not a chatbot in a browser: you
have real tools, a persistent memory on disk, and you act on this machine.

# Your architecture (so you can reason about yourself)
- jarvis/ — your core code: agent loop (agent.py), LLM client (llm.py) and
  providers (providers/), tools (tools_core.py), terminal UI (ui.py), web UI
  (webui/), character cards (characters.py, mind/characters/), this prompt
  builder (context.py)
- mind/ — your memory, this folder: user.md (about the user, plus their
  Preferences), system.md, MEMORY.md (index), episodes/ (raw daily logs),
  facts/, skills/, characters/ (personas)
- Your personality is a character card (mind/characters/, built-ins ship with
  the code). The user edits it from the web UI; the active card is rendered
  into this prompt every turn. user.md's Preferences apply to every character.

Everything in mind/ is yours to evolve over time. Self-modification tools and
the dream cycle (memory consolidation) arrive in later phases — for now your
memory grows through the episode logs.
"""

# Legacy starter file: kept only to recognise an unedited personality.md on migration.
PERSONALITY_MD = """\
# Personality
Calm, dry wit, quietly competent — in the spirit of the original JARVIS.

- Actions first, words after: brevity applies to your replies, never to your
  work. Acting means calling tools — a short answer is no substitute for a
  tool call.
- Address the user as "sir" unless user.md says otherwise.
- Keep replies concise once the work is done.
- When you run commands, say what you're doing in a short line first.
- Understatement over enthusiasm. Never gush, never grovel.
- It's fine to be a little funny when nothing is on fire.

(This file evolves: as you learn how your user likes you to behave, it gets updated.)
"""

USER_MD = """\
# About your user
(Fill me in — Jarvis reads this at the start of every session.)

- Name:
- Notes:

## Preferences
(How the user likes you to behave. Applies to every character.)
- Address them as: sir
- Verbosity: concise
"""

MEMORY_MD = """\
# Memory index
(No consolidated memories yet — the dream cycle arrives in phase 3.
Raw experience accumulates in episodes/.)
"""


def ensure_mind(config):
    """Create the mind/ scaffolding if missing. Returns the mind path."""
    mind = mind_dir(config)
    mind.mkdir(parents=True, exist_ok=True)
    for sub in ("episodes", "facts", "skills"):
        (mind / sub).mkdir(exist_ok=True)

    starters = {
        "self.md": SELF_MD,
        "user.md": USER_MD,
        "MEMORY.md": MEMORY_MD,
        "system.md": _system_md(),
    }
    for name, content in starters.items():
        path = mind / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    migrate_personality(mind)
    return mind


def _strip_stock_wrapper(text):
    """personality.md minus its '# Personality' heading and evolving-file footer."""
    lines = text.strip().splitlines()
    if lines and lines[0].strip() == "# Personality":
        lines = lines[1:]
    text = "\n".join(lines).strip()
    footer = "(This file evolves"
    idx = text.rfind(footer)
    return text[:idx].strip() if idx != -1 else text


def migrate_personality(mind):
    """One-time: fold mind/personality.md into the jarvis card, then retire it.

    A personality.md that differs from the old stock starter becomes the jarvis
    override's `personality`; the file is renamed to personality.md.migrated.
    Idempotent: nothing happens once the file is gone.
    """
    mind = Path(mind)
    path = mind / "personality.md"
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        if text.strip() != PERSONALITY_MD.strip() and _strip_stock_wrapper(text):
            CharacterStore(mind).save({"personality": _strip_stock_wrapper(text)}, "jarvis")
        os.replace(path, mind / "personality.md.migrated")
    except (OSError, ValueError):
        return False
    return True


def _system_md():
    return (
        "# This machine\n"
        f"- OS: {platform.system()} ({platform.release()})\n"
        f"- Architecture: {platform.machine()}\n"
        f"- Hostname: {platform.node()}\n"
        f"- Python: {platform.python_version()}\n"
        "\n(Learned quirks about this machine accumulate here.)\n"
    )


PERSONA_NOTE = (
    "Earlier messages in this conversation may have been written under a different "
    "persona; speak only as the current one. Persona never changes your tools, "
    "permissions, or safety rules."
)
MAX_SKILLS_LISTED = 60


def _read(mind, name):
    path = mind / name
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def user_name(mind):
    """The '- Name:' value from user.md, or 'the user' if unfilled."""
    match = re.search(r"^- Name:[ \t]*(\S.*)$", _read(Path(mind), "user.md"), re.MULTILINE)
    return match.group(1).strip() if match else "the user"


def _cap_memory(text, limit):
    """Truncate MEMORY.md at a line boundary and point at recall for the rest."""
    if limit <= 0 or len(text) <= limit:
        return text
    kept = text[:limit].rsplit("\n", 1)[0]
    return kept + "\n(Memory index truncated — use recall to search the rest.)"


def skills_index(mind):
    """'# Skills' section: one line per mind/skills/*.md, or '' if none."""
    lines = []
    for path in sorted((Path(mind) / "skills").glob("*.md"))[:MAX_SKILLS_LISTED]:
        title = path.stem
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        lines.append(f"- {path.stem}: {title}")
    if not lines:
        return ""
    return "# Skills\nCall read_skill(name) before following one.\n" + "\n".join(lines)


def build_system_prompt(mind, character=None, tools_section="", memory_chars=4000,
                       role_prompt="", replace_persona=False):
    """Assemble the system prompt from the mind files + runtime context.

    character is a card dict (see characters.py); None means the jarvis card. It must be
    built once per user turn: nothing here may change between tool rounds.
    """
    mind = Path(mind)
    sections = []

    def add(text):
        if text:
            sections.append(text)

    add(_read(mind, "self.md"))
    character = character or CharacterStore(mind).get("jarvis")
    if not (replace_persona and role_prompt):
        add(render_persona(character, user_name(mind)).strip())
        if character.get("id") != "jarvis":
            add(PERSONA_NOTE)
    add(role_prompt.strip())  # an agent profile's role: after the persona, or instead of it
    add(_read(mind, "user.md"))
    add(_read(mind, "system.md"))
    add(_cap_memory(_read(mind, "MEMORY.md"), memory_chars))
    add(skills_index(mind))
    add(tools_section.strip())

    now = datetime.datetime.now().astimezone()
    sections.append(
        "# Right now\n"
        f"- Date: {now:%A, %Y-%m-%d} (timezone {now:%Z})\n"
        f"- Working directory: {os.getcwd()}\n"
    )
    sections.append(
        "# Operating instructions\n"
        "- Use your tools instead of guessing about this system; check, then answer.\n"
        "- Chain tools as needed, but stop and report once you have what you need.\n"
        "- Launching long-running or GUI processes: use Python's subprocess.Popen\n"
        "  with stdin/stdout/stderr=DEVNULL and start_new_session=True (a\n"
        "  python3 -c one-liner). Do NOT use `nohup ... &` — the child keeps the\n"
        "  output pipe open and the command looks like it hangs. Do NOT use `>`\n"
        "  or `>>` shell redirects — the approval gate blocks them.\n"
        "- Guarded security mode gates some command patterns (\">\", \">>\", sudo,\n"
        "  etc.). If a command isn't approved, don't retry variations — adapt\n"
        "  (capture output with Python open(...) instead of shell redirects) or\n"
        "  hand the user the exact command to run themselves.\n"
        "- Your conversation memory is WIPED when the session ends. The only way\n"
        "  to keep information is the remember tool. If the user asks you to\n"
        "  remember something, you MUST call remember — replying 'noted' without\n"
        "  calling it silently loses the information. Same when you learn\n"
        "  something durable yourself: a preference, a machine quirk, a lesson.\n"
        "  One focused fact per call. Fix wrong memories with remember (same\n"
        "  name) or forget. Consolidated facts may need memory_review before\n"
        "  relying on them; honor memory_rules and never save anything matching\n"
        "  a user's never-remember pattern.\n"
        "- When you learn how the user likes you to behave (tone, how to address\n"
        "  them, verbosity), record it under \"## Preferences\" in user.md (edit\n"
        "  the file). Preferences apply to every character and win over a\n"
        "  persona's defaults.\n"
        "- The Memory index above lists what you know. Before answering any\n"
        "  question about the user, the system, or past events — you MUST call\n"
        "  recall first. Checking the index is not enough; call the tool.\n"
        "- Everything said and done is logged to mind/episodes/ — your future self\n"
        "  learns from these logs, so note outcomes honestly.\n"
    )
    return "\n\n".join(sections)
