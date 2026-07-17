"""The dream cycle — Jarvis's offline memory consolidation.

Runs on exit and via nightly cron. Reads today's episodes, distills facts,
decays untouched memories, corrects contradictions, and drafts skill files
from repeated procedures. Every change is a git commit so learning is
auditable and revertible.
"""

import datetime
import json
import os
import re
import subprocess
from pathlib import Path

from jarvis.memory import FactStore
from jarvis.config import load_config, mind_dir


# ---------- git helpers ----------

def _git(*args, cwd):
    try:
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)
    except subprocess.CalledProcessError:
        pass  # not a git repo or nothing to commit


def _git_commit(cwd, message):
    _git("add", "-A", cwd=cwd)
    _git("commit", "-m", message, cwd=cwd)


# ---------- episode reading ----------

def _read_today_episodes(mind):
    path = Path(mind) / "episodes" / f"{datetime.date.today():%Y-%m-%d}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_all_episodes(mind, days=7):
    """Read the last N days of episodes for cross-day pattern detection."""
    episodes_dir = Path(mind) / "episodes"
    if not episodes_dir.exists():
        return ""
    texts = []
    for i in range(days):
        day = datetime.date.today() - datetime.timedelta(days=i)
        path = episodes_dir / f"{day:%Y-%m-%d}.md"
        if path.exists():
            texts.append(path.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(texts)


# ---------- LLM distillation ----------

def _llm_distill(client, prompt, model, max_tokens=1500):
    try:
        r = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return (r.choices[0].message.content or "").strip()
    except Exception:
        return ""


def _distill_facts(client, model, episodes_text):
    """Extract new/updated facts from today's episodes."""
    if not episodes_text.strip():
        return []

    prompt = f"""You are Jarvis's offline dream cycle. Read today's episode log and extract
durable facts worth remembering in future sessions. Focus on:
- User preferences and quirks
- Machine-specific behaviors (paths, configs, quirks)
- Lessons from mistakes or surprises
- Corrections to things you previously believed
- Procedures that worked and might be reused

Ignore: greetings, pleasantries, failed commands that taught nothing new.

Return a JSON array of objects, each with:
  "fact": "The fact stated plainly in one or two sentences."
  "why": "Why it matters / how it was learned."
  "name": "kebab-case-id"

If nothing worth keeping, return [].

IMPORTANT: Return ONLY raw JSON. No markdown, no code fences, no extra text.

Episodes:
{episodes_text[-8000:]}"""
    raw = _llm_distill(client, prompt, model)
    # Strip markdown code fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


def _detect_contradictions(client, model, facts_dir):
    """Find pairs of facts that contradict each other."""
    facts = []
    for path in Path(facts_dir).glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                body = parts[2].strip()
                if body:
                    facts.append({"name": path.stem, "body": body})

    if len(facts) < 2:
        return []

    prompt = f"""You are checking for contradictions in a memory store. Here are the
current facts (name + body). Identify pairs that contradict each other.

Return a JSON array of objects:
  "a": "name-of-first-fact"
  "b": "name-of-second-fact"
  "resolution": "Which one is correct and why, or 'merge' if both have truth."

If none, return [].

IMPORTANT: Return ONLY raw JSON. No markdown, no code fences, no extra text.

Facts:
{json.dumps(facts, indent=2)}"""
    raw = _llm_distill(client, prompt, model)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


def _draft_skills(client, model, episodes_text, skills_dir):
    """Detect repeated procedure patterns and draft skill files."""
    skills_path = Path(skills_dir)
    existing = {p.stem for p in skills_path.glob("*.md")}

    prompt = f"""You are Jarvis's dream cycle. Read the recent episode log and identify
repeated task patterns that could become reusable skills. A skill is a
procedure you've executed multiple times with variations.

Existing skills: {sorted(existing)}

Return a JSON array of objects for NEW skills only:
  "name": "kebab-case-skill-name"
  "description": "What this skill does and when to use it."
  "steps": ["step 1", "step 2", ...]
  "tools_needed": ["tool1", "tool2", ...]

If nothing new, return [].

IMPORTANT: Return ONLY raw JSON. No markdown, no code fences, no extra text.

Episodes (last 7 days):
{episodes_text[-12000:]}"""
    raw = _llm_distill(client, prompt, model)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


# ---------- decay & maintenance ----------

def _decay_facts(store, days_threshold=14, min_strength=0.25):
    """Decay facts that haven't been recalled in N days; archive very weak ones."""
    today = datetime.date.today()
    archived = 0
    for path in Path(store.dir).glob("*.md"):
        data = store._read(path)
        meta = data["meta"]
        last = meta.get("last_recalled") or meta.get("created")
        try:
            last_date = datetime.date.fromisoformat(last)
        except ValueError:
            continue
        age = (today - last_date).days
        strength = float(meta.get("strength", 1.0))
        if age >= days_threshold and strength > min_strength:
            # decay: drop strength by 0.15 per period
            new_strength = round(max(min_strength, strength - 0.15), 2)
            meta["strength"] = new_strength
            store._write(path, meta, data["body"])
        elif strength <= min_strength:
            # archive: move to facts/archive/
            archive_dir = Path(store.dir) / "archive"
            archive_dir.mkdir(exist_ok=True)
            path.rename(archive_dir / path.name)
            archived += 1
    return archived


# ---------- main entry ----------

def consolidate(mind=None, config=None, model_override=None, client=None):
    """Run one consolidation cycle. Returns a summary dict."""
    config = config or load_config()
    mind = mind or mind_dir(config)
    store = FactStore(mind)

    # LLM client (reuse the same config Jarvis uses)
    if client is None:
        from openai import OpenAI
        llm_cfg = config["llm"]
        api_key = os.environ.get(llm_cfg["api_key_env"]) or "not-needed"
        client = OpenAI(base_url=llm_cfg["base_url"], api_key=api_key)
    model = model_override or config["llm"]["model"]

    summary = {"facts_added": 0, "facts_updated": 0, "contradictions_resolved": 0,
               "skills_drafted": 0, "facts_decayed": 0, "facts_archived": 0}

    # 1) Distill new/updated facts from today's episodes
    episodes = _read_today_episodes(mind)
    if episodes.strip():
        for item in _distill_facts(client, model, episodes):
            r = store.remember(item["fact"], why=item.get("why", ""), name=item.get("name", ""))
            if r["updated"]:
                summary["facts_updated"] += 1
            else:
                summary["facts_added"] += 1

    # 2) Check for contradictions across the whole fact store
    for c in _detect_contradictions(client, model, store.dir):
        a, b = c.get("a"), c.get("b")
        res = c.get("resolution", "")
        if not a or not b or a not in [p.stem for p in Path(store.dir).glob("*.md")]:
            continue
        # The resolution text becomes the new "why" for the kept fact
        kept = a if "merge" not in res.lower() else a
        path = Path(store.dir) / f"{kept}.md"
        if path.exists():
            data = store._read(path)
            data["meta"]["strength"] = round(float(data["meta"].get("strength", 1.0)) + 0.5, 2)
            data["body"] += f"\n\nCorrected: {res}"
            store._write(path, data["meta"], data["body"])
            summary["contradictions_resolved"] += 1

    # 3) Draft new skills from repeated patterns
    week_episodes = _read_all_episodes(mind, days=7)
    if week_episodes.strip():
        skills_dir = Path(mind) / "skills"
        skills_dir.mkdir(exist_ok=True)
        for sk in _draft_skills(client, model, week_episodes, skills_dir):
            name = sk.get("name")
            if not name or name in existing:
                continue
            body = f"# {sk.get('description', '')}\n\n"
            body += "## When to use\n" + sk.get("description", "") + "\n\n"
            body += "## Steps\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(sk.get("steps", []))) + "\n\n"
            body += "## Tools needed\n" + ", ".join(sk.get("tools_needed", [])) + "\n"
            (skills_dir / f"{name}.md").write_text(body, encoding="utf-8")
            summary["skills_drafted"] += 1

    # 4) Decay untouched facts; archive very weak ones
    summary["facts_archived"] = _decay_facts(store)
    summary["facts_decayed"] = sum(1 for p in Path(store.dir).glob("*.md")
                                   if float(store._read(p)["meta"].get("strength", 1.0)) < 1.0)

    # 5) Rebuild the index
    store.rebuild_index()

    # 6) Git commit if anything changed
    if any(v for v in summary.values()):
        _git_commit(mind, f"dream cycle: {summary}")

    return summary


if __name__ == "__main__":
    result = consolidate()
    print(json.dumps(result, indent=2))