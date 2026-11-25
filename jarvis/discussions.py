"""Discussions: agenda-driven group chats between several agents (spec 1.1.2 section 4).

Layout, top to bottom:
  constants      modes, outcomes, presets, templates (served to the UI by /api/discussions/meta)
  validation     validate_spec(): one place that checks a create/patch request
  DiscussionStore  mind/discussions/<id>/discussion.json + transcript.jsonl
  Engine         one worker thread per running discussion: items -> rounds -> speaking turns
  DiscussionManager  what routes and WebState use: create/list/get/patch/control/message/export

Each speaking turn is a normal Jarvis run (kind="discussion", persist=False) with its own
provider/model and research tools only. The transcript is rendered into messages for it:
its own earlier turns as assistant messages, everybody else as user messages "Name: text".
"""

import copy
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import keystore
from .config import mind_dir
from .events import Event
from .providers import PRESETS
from .redaction import redact_text
from .runtime import AgentProfile, apply_profile_config, llm_overrides

MODES = {
    "round_robin": ("Round robin", "Everyone speaks in turn and sees the whole transcript."),
    "parallel": ("Parallel", "Everyone answers at the same time without seeing the others, then all answers are revealed."),
    "moderated": ("Moderated", "A moderator model picks who speaks next, decides when an item is done and can ask you."),
    "debate": ("Debate", "Sides for and against alternate; an optional judge sums up each item."),
}
OUTCOMES = {"decision": "A decision", "plan": "A plan", "report": "A report", "ideas": "A list of ideas"}
STANCES = ("", "for", "against", "judge")
COLORS = ["#2dd4e8", "#f59e0b", "#a78bfa", "#34d399", "#fb7185", "#60a5fa"]
MAX_PARTICIPANTS, MIN_PARTICIPANTS, MAX_ITEMS, MAX_ROUNDS = 6, 2, 12, 6
MAX_FAILURES = 3  # a participant failing this many turns in a row sits out the rest of the discussion

# Tools a discussion speaker may use (whichever of these exist in the registry).
RESEARCH_TOOLS = {"web_search", "fetch_page", "internet_search", "webpage_scrape", "recall",
                  "read_file", "grep", "glob", "list_dir", "get_current_time", "calculate"}

PRESET_ROLES = [
    {"name": "Researcher", "role": "You find facts. Search and read sources, bring concrete evidence, numbers and links, and say how sure you are."},
    {"name": "Skeptic", "role": "You stress-test ideas. Look for weak assumptions, risks and missing evidence, and say plainly what would change your mind."},
    {"name": "Strategist", "role": "You think about goals, trade-offs and sequencing. Turn the discussion into options with a clear recommendation."},
    {"name": "Domain expert", "role": "You bring deep practical knowledge of the subject. Correct misconceptions and explain how it works in practice."},
    {"name": "Devil's advocate", "role": "You argue the opposite of the emerging consensus, as strongly as honesty allows, so weak spots show up early."},
    {"name": "Summarizer", "role": "You keep the group on track: restate what was agreed, name what is still open, and propose the next step."},
]

TEMPLATES = [
    {"id": "product", "title": "Product idea review", "icon": "sparkle",
     "text": "Pressure-test a product idea before building it.",
     "goal": "Decide whether this product idea is worth building, and what the first version should be.",
     "items": ["Who is the user and what problem do they have?", "What already exists, and how are we different?",
               "What is the smallest first version?"],
     "outcome": "decision", "mode": "round_robin", "rounds": 1,
     "participants": ["Researcher", "Skeptic", "Strategist"]},
    {"id": "research", "title": "Research a question", "icon": "search",
     "text": "Several researchers dig in independently, then compare.",
     "goal": "Answer the question with sources, and say how confident we are.",
     "items": ["What do the best sources say?", "Where do they disagree, and why?"],
     "outcome": "report", "mode": "parallel", "rounds": 2,
     "participants": ["Researcher", "Domain expert", "Summarizer"]},
    {"id": "debate", "title": "Debate: X vs Y", "icon": "discussions",
     "text": "Two sides argue it out and a judge sums up.",
     "goal": "Decide between X and Y, with the strongest argument for each side.",
     "items": ["The case for X", "The case for Y", "Which one, and what would change that?"],
     "outcome": "decision", "mode": "debate", "rounds": 2,
     "participants": ["Strategist", "Devil's advocate", "Summarizer"],
     "stances": ["for", "against", "judge"]},
]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------- validation

def provider_ready(provider, state=None):
    """(ok, hint) for a provider id: is there a key (saved or env) / sign-in / local server?"""
    preset = PRESETS.get(provider)
    if provider in ("", None) or preset is None:
        return True, ""  # current model / custom endpoint: nothing to check here
    if preset.get("backend") == "chatgpt":
        try:
            ok = bool(state.chatgpt_status().get("signed_in")) if state else True
        except Exception:
            ok = False
        return ok, "" if ok else "Not signed in to ChatGPT"
    env = preset.get("api_key_env") or ""
    if preset.get("kind") == "api_key" and env:
        if keystore.get(env) or os.environ.get(env):
            return True, ""
        return False, f"No API key for {provider} ({env})"
    return True, ""


def _clean_text(value, limit):
    return str(value or "").strip()[:limit]


def validate_spec(spec, state=None, previous=None):
    """Check a create/patch body. Returns (clean_fields, warnings); raises ValueError(message).

    `previous` is the stored discussion when patching (its ids and colours are kept).
    """
    errors, warnings = [], []
    agenda_in = spec.get("agenda") or {}
    goal = _clean_text(agenda_in.get("goal", spec.get("goal")), 2000)
    if not goal:
        errors.append("The goal is required: say what this discussion should achieve.")
    items = []
    for raw in agenda_in.get("items", spec.get("items")) or []:
        raw = {"title": raw} if isinstance(raw, str) else (raw or {})
        title = _clean_text(raw.get("title"), 300)
        if title:
            items.append({"title": title, "notes": _clean_text(raw.get("notes"), 1000),
                          "summary": _clean_text(raw.get("summary"), 6000)})
    if not items:
        errors.append("Add at least one agenda item.")
    if len(items) > MAX_ITEMS:
        errors.append(f"At most {MAX_ITEMS} agenda items.")
    outcome = agenda_in.get("outcome", spec.get("outcome")) or "decision"
    if outcome not in OUTCOMES:
        errors.append(f"Unknown outcome: {outcome}")
    mode = spec.get("mode") or "round_robin"
    if mode not in MODES:
        errors.append(f"Unknown mode: {mode}")
    try:
        rounds = int(spec.get("rounds_per_item", 2))
    except (TypeError, ValueError):
        rounds = 0
    if not 1 <= rounds <= MAX_ROUNDS:
        errors.append(f"Rounds per item must be 1 to {MAX_ROUNDS}.")

    old = {p["id"]: p for p in (previous or {}).get("participants", [])}
    people, seen = [], set()
    for index, raw in enumerate(spec.get("participants") or []):
        name = _clean_text(raw.get("name"), 40)
        if not name:
            errors.append(f"Participant {index + 1} needs a name.")
            continue
        if name.lower() in seen:
            errors.append(f"Two participants are called {name}. Names must be unique.")
        seen.add(name.lower())
        provider = _clean_text(raw.get("provider"), 60)
        if provider and provider not in PRESETS and provider != "custom":
            errors.append(f"{name}: unknown provider {provider!r}.")
        ok, hint = provider_ready(provider, state)
        if not ok:
            warnings.append(f"{name}: {hint}. Add it in Settings or this participant will fail.")
        stance = raw.get("stance") or ""
        if stance not in STANCES:
            errors.append(f"{name}: unknown stance {stance!r}.")
        pid = raw.get("id") if raw.get("id") in old else f"p{index + 1}"
        while pid in {p["id"] for p in people}:
            pid += "x"
        people.append({"id": pid, "name": name, "role": _clean_text(raw.get("role"), 3000),
                       "provider": provider, "model": _clean_text(raw.get("model"), 200),
                       "tools": bool(raw.get("tools", True)), "stance": stance,
                       "color": _clean_text(raw.get("color"), 20) or COLORS[index % len(COLORS)],
                       "agent": _clean_text(raw.get("agent"), 80)})
    if not MIN_PARTICIPANTS <= len(people) <= MAX_PARTICIPANTS:
        errors.append(f"A discussion needs {MIN_PARTICIPANTS} to {MAX_PARTICIPANTS} participants.")
    if mode == "debate":
        debaters = [p for p in people if p["stance"] != "judge"]
        for index, person in enumerate(debaters):  # unset stances alternate for / against
            person["stance"] = person["stance"] or ("for" if index % 2 == 0 else "against")
        if len(debaters) < 2 or not any(p["stance"] == "for" for p in debaters) \
                or not any(p["stance"] == "against" for p in debaters):
            errors.append("A debate needs at least one participant for and one against.")
    else:
        for person in people:
            person["stance"] = ""
    moderator = spec.get("moderator") or {}
    moderator = {"provider": _clean_text(moderator.get("provider"), 60), "model": _clean_text(moderator.get("model"), 200)}
    if moderator["provider"] and moderator["provider"] not in PRESETS and moderator["provider"] != "custom":
        errors.append(f"Moderator: unknown provider {moderator['provider']!r}.")
    ok, hint = provider_ready(moderator["provider"], state)
    if not ok:
        warnings.append(f"Moderator: {hint}.")
    if errors:
        raise ValueError(" ".join(errors))
    title = _clean_text(spec.get("title"), 120) or (goal if len(goal) <= 60 else goal[:57].rstrip() + "...")
    return {"title": title, "agenda": {"goal": goal, "items": items, "outcome": outcome}, "mode": mode,
            "rounds_per_item": rounds, "participants": people, "moderator": moderator}, warnings


# ---------------------------------------------------------------- storage

class DiscussionStore:
    """mind/discussions/<id>/discussion.json and transcript.jsonl."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def dir(self, discussion_id):
        return self.root / discussion_id

    def ids(self):
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "discussion.json").is_file())

    def load(self, discussion_id):
        try:
            data = json.loads((self.dir(discussion_id) / "discussion.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, discussion):
        with self.lock:
            folder = self.dir(discussion["id"])
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / "discussion.json.tmp"
            tmp.write_text(json.dumps(discussion, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(folder / "discussion.json")

    def append(self, discussion_id, entry):
        with self.lock:
            folder = self.dir(discussion_id)
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / "transcript.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def transcript(self, discussion_id):
        path = self.dir(discussion_id) / "transcript.jsonl"
        out = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a half-written last line after a crash
        except OSError:
            pass
        return out

    def delete(self, discussion_id):
        import shutil
        shutil.rmtree(self.dir(discussion_id), ignore_errors=True)


# ---------------------------------------------------------------- prompts

RULES = """\
# Rules
- Be concise: at most about 180 words unless someone asks for more.
- Build on, or challenge, what others said, and refer to them by name.
- Cite a source (link or file) for any fact you got from a tool; do not invent sources.
- Do not repeat points already made.
- Stay on the current agenda item.
- If you have nothing new to add, reply with exactly: PASS
- Write plain Markdown. Do not start with your own name."""

MODERATOR_SYSTEM = """\
You moderate a group discussion between AI participants and one human. Choose who speaks
next. Reply with ONLY a JSON object, no prose, no code fence:
{"next": "<participant id>" | "user" | "none", "advance": true | false, "reason": "<short>", "question": "<only when next is user>"}
- "advance": true when the current agenda item has been covered well enough to summarize and move on.
- "next": "user" only when you need the human's input to proceed; put the question in "question".
- Prefer participants who have spoken less, and the one best placed to answer what was just said."""

SUMMARY_SYSTEM = """\
You write the record of a group discussion. Be factual and compact; never invent points
nobody made. Use Markdown."""


def _json_object(text):
    """The first JSON object found in a model reply, or None."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    for candidate in (text, *re.findall(r"\{.*\}", text, flags=re.S)):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _is_pass(text):
    return bool(re.fullmatch(r"\W*PASS\W*", (text or "").strip(), flags=re.I))


def _short(text, limit=300):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def provider_label(provider):
    return ((PRESETS.get(provider, {}).get("label") or provider or "").split(" — ")[0]) or "current model"


# ---------------------------------------------------------------- engine

class Engine:
    """Runs one discussion in a worker thread; reacts to control flags between speaking turns."""

    def __init__(self, manager, discussion):
        self.manager = manager
        self.state = manager.state
        self.store = manager.store
        self.d = discussion
        self.id = discussion["id"]
        self.cv = threading.Condition()
        self.thread = None
        self.paused = self.stopped = self.skip = self.concluding = False
        self.waiting = False
        self.forced = []          # participant ids the user asked to speak next
        self.addressed = set()    # user message ids a speaker has already answered
        self.user_spoke = threading.Event()
        self.done = set()         # slots ("item:round:pid") already played (survives pause/resume)
        self.instances = {}       # participant id -> Jarvis
        self.sid_map = {}         # session id -> participant id
        self.live = {}            # message id -> {participant, text, tools} while streaming
        self.fails = {}           # participant id -> consecutive failures
        self.failed_in_item = set()  # participants that failed during the current item (moderator skips them)
        self.interrupt = threading.Event()
        self.lock = manager.lock  # one lock for the shared discussion dict
        self._watch = None
        self._stats = {}          # participant id -> [stats of the current turn]
        self._tools = {}          # participant id -> [tool records of the current turn]
        for entry in self.store.transcript(self.id):
            if entry.get("slot"):
                self.done.add(entry["slot"])

    # ----- lifecycle

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, conclude=False):
        with self.cv:
            if self.alive():
                return False
            self.paused = self.stopped = self.skip = False
            self.concluding = conclude
            self.interrupt.clear()
            self.thread = threading.Thread(target=self._main, name=f"discussion-{self.id}", daemon=True)
        self.thread.start()
        return True

    def _main(self):
        d = self.d
        self._watch = self.state.jarvis.events.subscribe(self._on_event)
        try:
            self._set_status("running")
            while d["current_item"] < len(d["agenda"]["items"]) and not (self.stopped or self.concluding):
                index = d["current_item"]
                self._alive_participants()  # pauses (and waits) when nobody is able to speak
                if self.stopped or self.concluding:
                    break
                self._run_item(index)
                if self.stopped:
                    break
                self._finish_item(index)
                if self.concluding:
                    break
                self.skip = False
                self.interrupt.clear()
                d["current_item"] = index + 1
                self._save()
            if self.concluding and d["current_item"] < len(d["agenda"]["items"]):
                self._finish_item(d["current_item"])
            if self.stopped:
                self._note("Stopped by you. The discussion is over; use Conclude to write the minutes.")
                self._set_status("finished")
            else:
                d["current_item"] = len(d["agenda"]["items"])
                self._write_minutes()
                self._set_status("finished")
        except Exception as error:  # the engine itself broke: keep the transcript, pause
            self._note(f"The discussion engine hit an error and paused: {error}", level="error")
            self._set_status("paused")
        finally:
            self.state.jarvis.events.unsubscribe(self._watch)
            self._close_instances()

    def _close_instances(self):
        for sid in list(self.sid_map):
            try:
                self.state.runtimes.close(sid)
            except Exception:
                pass
        self.instances.clear()
        self.sid_map.clear()

    # ----- controls (called from routes)

    def pause(self):
        with self.cv:
            if self.d["status"] in ("running", "waiting"):
                self.paused = True
        self._set_status("paused")

    def resume(self):
        with self.cv:
            self.paused = False
            self.cv.notify_all()
        if self.alive():
            self._set_status("running")
            self.user_spoke.set()
        else:
            self.start()

    def next_item(self):
        with self.cv:
            self.skip, self.paused = True, False
            self.cv.notify_all()
        self.user_spoke.set()
        self._interrupt()
        if not self.alive():
            self.start()

    def conclude(self):
        with self.cv:
            self.concluding, self.paused = True, False
            self.cv.notify_all()
        self.user_spoke.set()
        self._interrupt()
        if not self.alive():
            self.start(conclude=True)

    def stop(self):
        with self.cv:
            self.stopped, self.paused = True, False
            self.cv.notify_all()
        self.user_spoke.set()
        self._interrupt()

    def _interrupt(self):
        """Cancel whoever is speaking right now (their partial text is kept)."""
        self.interrupt.set()
        for jarvis in list(self.instances.values()):
            if jarvis.busy:
                jarvis.cancel()

    def user_message(self, entry):
        """A user entry was added: wake a waiting engine, honour @mentions."""
        for pid in self.mentions(entry["text"]):
            if pid not in self.forced:
                self.forced.append(pid)
        self.user_spoke.set()

    def mentions(self, text):
        """Participant ids named with @Name (full name or first word), in the order mentioned."""
        low = (text or "").lower()
        found = []
        for person in self.d["participants"]:
            name = person["name"].lower()
            for needle in (name, name.split()[0]):
                match = re.search(r"@" + re.escape(needle) + r"(?!\w)", low)
                if match:
                    found.append((match.start(), person["id"]))
                    break
        return [pid for _, pid in sorted(found)]

    # ----- status / events / transcript

    def _emit(self, kind, **data):
        self.state.jarvis.events.emit(Event(kind, self.id, data={"discussion_id": self.id, **data}))

    def _save(self):
        with self.lock:
            self.d["updated"] = now_iso()
            self.store.save(self.d)

    def _set_status(self, status, **extra):
        with self.lock:
            self.d["status"] = status
            self.waiting = status == "waiting"
        self._save()
        self._emit("discussion_status", status=status, current_item=self.d["current_item"], **extra)

    def _add(self, entry):
        """Append a transcript entry (redacted), persist and announce it."""
        entry.setdefault("id", new_id("m"))
        entry.setdefault("ts", now_iso())
        entry.setdefault("item", self.d["current_item"])
        entry["text"] = redact_text(entry.get("text") or "")
        self.store.append(self.id, entry)
        if entry.get("slot"):
            self.done.add(entry["slot"])
        self._emit("discussion_message", message=entry)
        return entry

    def _note(self, text, level="info", **extra):
        return self._add({"kind": "system", "level": level, "speaker": "system", "name": "System", "text": text, **extra})

    def _transcript(self):
        return self.store.transcript(self.id)

    # ----- flow control helpers

    def _checkpoint(self):
        """Wait while paused. True when the current item should be abandoned."""
        with self.cv:
            while self.paused and not (self.stopped or self.skip or self.concluding):
                self.cv.wait(0.4)
        return self.stopped or self.skip or self.concluding

    def _people(self):
        return self.d["participants"]

    def _person(self, pid):
        return next((p for p in self._people() if p["id"] == pid), None)

    def _active(self):
        return [p for p in self._people() if self.fails.get(p["id"], 0) < MAX_FAILURES]

    # ----- one agenda item

    def _run_item(self, index):
        self.failed_in_item.clear()
        self._emit("discussion_status", status=self.d["status"], current_item=index)
        mode = self.d["mode"]
        if mode == "parallel":
            self._item_parallel(index)
        elif mode == "moderated":
            self._item_moderated(index)
        else:
            self._item_sequential(index)

    def _order(self):
        people = self._active()
        if self.d["mode"] != "debate":
            return people
        fors = [p for p in people if p["stance"] == "for"]
        againsts = [p for p in people if p["stance"] == "against"]
        order = []
        for pair in zip(fors, againsts):
            order.extend(pair)
        extra = fors[len(againsts):] + againsts[len(fors):]
        return order + extra  # judges only speak when asked or when summarizing

    def _speak_forced(self, index, rnd):
        """Turns the user asked for with @mentions (they do not count towards the rounds)."""
        while self.forced and not self._checkpoint():
            person = self._person(self.forced.pop(0))
            if person and self.fails.get(person["id"], 0) < MAX_FAILURES:
                self._sequential_turn(person, index, rnd, slot=None, forced=True)

    def _item_sequential(self, index):
        rounds = self.d["rounds_per_item"]
        for rnd in range(rounds):
            if self._checkpoint() or not self._alive_participants():
                return
            passed, spoke = 0, 0
            for person in self._order():
                self._speak_forced(index, rnd)
                if self._checkpoint() or not self._alive_participants():
                    return
                slot = f"{index}:{rnd}:{person['id']}"
                if slot in self.done:
                    continue
                entry = self._sequential_turn(person, index, rnd, slot)
                spoke += 1
                passed += 1 if entry and entry.get("pass") else 0
            self._speak_forced(index, rnd)
            if spoke and passed == spoke:
                return  # a whole round of PASS: nothing new to say

    def _alive_participants(self):
        if self._active():
            return True
        self._note("Every participant has failed repeatedly, so the discussion is paused. "
                   "Fix the provider settings, then press Resume.", level="error")
        self.paused = True
        self._set_status("paused")
        if self._checkpoint():
            return False
        self.fails.clear()  # the user fixed something and resumed: everyone gets another chance
        return True

    def _sequential_turn(self, person, index, rnd, slot, forced=False):
        transcript = self._transcript()
        fresh = [e for e in transcript if e.get("kind") == "user" and e["id"] not in self.addressed]
        entry = self._speak(person, index, rnd, slot, transcript, fresh, forced)
        for e in fresh:
            self.addressed.add(e["id"])
        if entry:
            self._add(entry)
        return entry

    def _item_parallel(self, index):
        rounds = self.d["rounds_per_item"]
        for rnd in range(rounds):
            self._speak_forced(index, rnd)
            if self._checkpoint() or not self._alive_participants():
                return
            todo = [p for p in self._active() if f"{index}:{rnd}:{p['id']}" not in self.done]
            if not todo:
                continue
            transcript = self._transcript()  # everyone sees the same snapshot: no anchoring
            fresh = [e for e in transcript if e.get("kind") == "user" and e["id"] not in self.addressed]
            with ThreadPoolExecutor(max_workers=len(todo)) as pool:
                futures = [pool.submit(self._speak, p, index, rnd, f"{index}:{rnd}:{p['id']}", transcript, fresh, False)
                           for p in todo]
                results = [f.result() for f in futures]
            for e in fresh:
                self.addressed.add(e["id"])
            for entry in results:  # revealed together, in participant order
                if entry:
                    self._add(entry)
            if self.interrupt.is_set():
                return

    def _item_moderated(self, index):
        people = self._people()
        cap = self.d["rounds_per_item"] * len(people) + len(people)
        bad_json = 0
        for step in range(cap):
            self._speak_forced(index, step)
            if self._checkpoint() or not self._alive_participants():
                return
            decision = self._decide(index, step)
            if decision is None:
                bad_json += 1
                if bad_json == 1:
                    self._note("The moderator's reply was not valid JSON; falling back to round robin for this item.")
                decision = self._fallback_decision(index)
            if decision["advance"] and step > 0:
                self._add({"kind": "moderator", "speaker": "moderator", "name": "Moderator",
                           "text": f"Moving on: {decision['reason'] or 'this item is covered.'}"})
                return
            if decision["next"] == "user":
                self._wait_for_user(decision["question"] or decision["reason"] or "What do you think?")
                continue
            person = self._person(decision["next"]) or self._fallback_person(index)
            if person is None:
                return
            if decision["reason"]:
                self._add({"kind": "moderator", "speaker": "moderator", "name": "Moderator",
                           "text": f"{person['name']}: {decision['reason']}"})
            self._sequential_turn(person, index, step, slot=f"{index}:m{step}:{person['id']}")

    def _wait_for_user(self, question):
        self._add({"kind": "message", "speaker": "moderator", "name": "Moderator", "text": question, "asks_user": True})
        self.user_spoke.clear()
        self._set_status("waiting")
        while not (self.user_spoke.wait(0.4) or self.stopped or self.skip or self.concluding):
            pass
        self.user_spoke.clear()
        if not (self.stopped or self.skip or self.concluding or self.paused):
            self._set_status("running")

    def _decide(self, index, step):
        """Ask the moderator model who speaks next. None when its reply cannot be parsed."""
        people = [p for p in self._active() if p["id"] not in self.failed_in_item]
        if not people:
            return {"next": "none", "advance": True, "reason": "Nobody left who can speak on this item.", "question": ""}
        counts = self._turn_counts(index)
        roster = "\n".join(f"- {p['id']}: {p['name']} ({_short(p['role'], 80)}) spoke {counts.get(p['id'], 0)} time(s)" for p in people)
        prompt = (f"Goal: {self.d['agenda']['goal']}\nCurrent item {index + 1}/{len(self.d['agenda']['items'])}: "
                  f"{self.d['agenda']['items'][index]['title']}\nTarget: about {self.d['rounds_per_item']} turn(s) each. Step {step + 1}.\n\n"
                  f"Participants:\n{roster}\n\n"
                  + ("Do not pick anyone not listed: they failed on this item.\n\n" if self.failed_in_item else "")
                  + f"Transcript of this item so far:\n{self._render_item(index, 6000) or '(nobody has spoken yet)'}")
        try:
            reply = self._complete(None, MODERATOR_SYSTEM, prompt, who="Moderator")
        except Exception as error:
            self._note(f"The moderator failed: {error}. Using round robin for this step.", level="error")
            return self._fallback_decision(index)
        data = _json_object(reply)
        if not data:
            return None
        target = str(data.get("next") or "").strip()
        ids = {p["id"] for p in people}
        if target not in ids and target not in ("user", "none"):
            by_name = next((p["id"] for p in people if p["name"].lower() == target.lower()), "")
            target = by_name or ""
        return {"next": target, "advance": bool(data.get("advance")) or target == "none",
                "reason": _short(data.get("reason"), 240), "question": _short(data.get("question"), 400)}

    def _turn_counts(self, index):
        counts = {}
        for e in self._transcript():
            if e.get("item") == index and e.get("kind") == "message" and e.get("slot"):
                counts[e["speaker"]] = counts.get(e["speaker"], 0) + 1
        return counts

    def _fallback_person(self, index):
        counts = self._turn_counts(index)
        people = [p for p in self._active() if p["id"] not in self.failed_in_item]
        return min(people, key=lambda p: counts.get(p["id"], 0), default=None)

    def _fallback_decision(self, index):
        person = self._fallback_person(index)
        counts = self._turn_counts(index)
        done = person is None or counts.get(person["id"], 0) >= self.d["rounds_per_item"]
        return {"next": person["id"] if person else "", "advance": done, "reason": "", "question": ""}

    # ----- a speaking turn

    def _profile(self, person):
        cfg = self.state.jarvis.config
        names = self.state.jarvis.registry.tool_names()
        allowed = RESEARCH_TOOLS if person["tools"] else set()
        profile = AgentProfile(
            kind="discussion", owner=self.id, title=f"{self.d['title']} — {person['name']}",
            replace_persona=True, max_tool_rounds=8, persist=False, allow_spawn=False,
            hidden_tools={n for n in names if n not in allowed},
            llm=llm_overrides(person["provider"], person["model"], cfg) if person["provider"] else None)
        if person["provider"] == "" and person["model"]:
            profile.llm = {"model": person["model"]}
        if person.get("agent") and person["tools"]:
            try:  # an installed Agent contributes its role text and private tools
                agent = self.state.agents.profile(person["agent"])
                profile.core_tools = list(getattr(agent, "core_tools", []) or [])
                profile.extra_tools = dict(getattr(agent, "extra_tools", {}) or {})
            except Exception as error:
                self._note(f"Could not load Agent {person['agent']!r} for {person['name']}: {error}", level="error")
        return profile

    def _instance(self, person):
        jarvis = self.instances.get(person["id"])
        if jarvis is None:
            jarvis = self.state.runtimes.create(self._profile(person))
            jarvis.config["agent"]["tool_mode"] = "all"  # research tools directly visible, no find_tools detour
            jarvis.exposure.refresh()
            self.instances[person["id"]] = jarvis
            self.sid_map[jarvis.session_id] = person["id"]
        return jarvis

    def _on_event(self, event):
        """Tool and stats events of our participants' private sessions -> discussion events."""
        pid = self.sid_map.get(event.session_id)
        if not pid:
            return
        data = event.data
        if event.kind == "assistant_stats":
            self._stats.setdefault(pid, []).append(data.get("stats") or {})
        elif event.kind in ("tool_started", "tool_finished"):
            record = {"name": data.get("name"), "args": data.get("args"), "via": data.get("via", "")}
            tools = self._tools.setdefault(pid, [])
            if event.kind == "tool_started":
                tools.append({**record, "status": "running"})
            else:
                running = next((t for t in tools if t["name"] == record["name"] and t["status"] == "running"), None)
                if running is None:
                    running = {**record}
                    tools.append(running)
                text = data.get("result")
                running["result"] = redact_text(text if isinstance(text, str) else json.dumps(text, default=str))[:2000]
                running["status"] = "error" if running["result"].startswith("Error") else "done"
            mid = next((m for m, v in self.live.items() if v["participant"] == pid), "")
            self._emit("discussion_tool", participant=pid, message_id=mid, phase=event.kind,
                       tool=tools[-1] if tools else record)

    def _speak(self, person, index, rnd, slot, transcript, fresh, forced):
        """One speaking turn. Returns a transcript entry (not yet added), or None."""
        mid = new_id("m")
        entry = self._speak_inner(mid, person, index, rnd, slot, transcript, fresh, forced)
        if entry is None:
            self._emit("discussion_dropped", participant=person["id"], message_id=mid)
        return entry

    def _speak_inner(self, mid, person, index, rnd, slot, transcript, fresh, forced):
        pid = person["id"]
        self.live[mid] = {"participant": pid, "text": "", "item": index}
        self._stats[pid], self._tools[pid] = [], []
        self._emit("discussion_speaker", participant=pid, message_id=mid, item=index, round=rnd)
        pieces = []

        def on_text(token):
            pieces.append(token)
            self.live[mid]["text"] = "".join(pieces)
            self._emit("discussion_delta", participant=pid, message_id=mid, content=token)

        entry = {"id": mid, "kind": "message", "speaker": pid, "name": person["name"], "item": index, "round": rnd,
                 "provider": person["provider"], "color": person["color"]}
        if slot:
            entry["slot"] = slot
        try:
            if self.stopped or (self.interrupt.is_set() and (self.skip or self.concluding)):
                return None
            jarvis = self._instance(person)
            entry["model"] = jarvis.llm.model
            entry["provider"] = person["provider"] or jarvis.config["llm"].get("provider", "")
            jarvis.profile.system_prompt = self._system_prompt(person, index)
            history, ask = self._messages(person, index, transcript, fresh, rnd, forced)
            jarvis.history = history
            reply = jarvis.ask(ask, on_text=on_text)
            if self.interrupt.is_set() and (self.stopped or self.skip or self.concluding):
                reply = "".join(pieces).strip()
                if not reply:
                    return None
                entry["interrupted"] = True
            self.fails[pid] = 0
        except Exception as error:
            return self._failure(person, error, entry, slot)
        finally:
            self.live.pop(mid, None)
        entry["text"] = re.sub(r"\n+\W*PASS\W*$", "", (reply or "").strip()).strip()  # models sometimes append PASS
        if _is_pass(entry["text"]):
            entry["pass"] = True
        if forced:
            entry["forced"] = True
        entry["stats"] = self._stats.pop(pid, [])
        entry["tools"] = self._tools.pop(pid, [])
        if not entry["text"] and not entry["tools"]:
            return None
        return entry

    def _failure(self, person, error, entry, slot):
        message = getattr(error, "message", None) or str(error) or type(error).__name__
        self.fails[person["id"]] = self.fails.get(person["id"], 0) + 1
        self.failed_in_item.add(person["id"])
        text = f"{provider_label(person['provider'] or self.state.jarvis.config['llm'].get('provider'))} ({person['name']}) failed: {_short(redact_text(message), 400)}"
        if self.fails[person["id"]] >= MAX_FAILURES:
            text += f" {person['name']} will sit out the rest of the discussion."
        self._note(text, level="error", participant=person["id"], slot=slot, failed=True)
        self._stats.pop(person["id"], None)
        self._tools.pop(person["id"], None)
        return None

    def _agenda_text(self, index):
        lines = []
        for i, item in enumerate(self.d["agenda"]["items"]):
            mark = "done" if i < index else "CURRENT" if i == index else "upcoming"
            line = f"{i + 1}. [{mark}] {item['title']}"
            if item.get("notes"):
                line += f" — {item['notes']}"
            lines.append(line)
        return "\n".join(lines)

    def _system_prompt(self, person, index):
        d = self.d
        others = [p for p in d["participants"] if p["id"] != person["id"]]
        roster = "\n".join(f"- {p['name']}: {_short(p['role'], 120) or 'participant'}" for p in others)
        role = person["role"] or "You are a thoughtful participant."
        parts = [f"You are {person['name']}, one participant in a structured group discussion between several AI "
                 f"participants and one human user. Everyone sees the shared transcript.",
                 f"# Goal\n{d['agenda']['goal']}\nExpected outcome: {OUTCOMES.get(d['agenda']['outcome'], '')}.",
                 f"# Agenda\n{self._agenda_text(index)}",
                 f"# Your role\n{role}"]
        if person["stance"] == "for":
            parts.append("# Your stance\nYou argue FOR the proposition. Make the strongest honest case; concede only what is true.")
        elif person["stance"] == "against":
            parts.append("# Your stance\nYou argue AGAINST the proposition. Make the strongest honest case; concede only what is true.")
        elif person["stance"] == "judge":
            parts.append("# Your stance\nYou are the judge: weigh both sides fairly and say who argued better and why.")
        parts.append(f"# Other participants\n{roster}\n- the user (the human): may speak at any time; answer them directly.")
        parts.append(RULES)
        if not person["tools"]:
            parts.append("You have no tools in this discussion; rely on what you know and what was said.")
        return "\n\n".join(parts)

    def _render_entry(self, e):
        return f"{e.get('name', '?')}: {e.get('text', '')}"

    def _render_item(self, index, limit=12000):
        lines = [self._render_entry(e) for e in self._transcript()
                 if e.get("item") == index and e.get("kind") in ("message", "user") and not e.get("pass")]
        text = "\n\n".join(lines)
        return text if len(text) <= limit else "…" + text[-limit:]

    def _messages(self, person, index, transcript, fresh, rnd, forced):
        """(history, ask_text): earlier items as summaries, this item as role-tagged messages."""
        history = []

        def push(role, text):
            if history and history[-1]["role"] == role:
                history[-1]["content"] += "\n\n" + text
            else:
                history.append({"role": role, "content": text})

        items = self.d["agenda"]["items"]
        for i in range(index):
            if items[i].get("summary"):
                push("user", f"[Summary of agenda item {i + 1}: {items[i]['title']}]\n{items[i]['summary']}")
        for e in transcript:
            if e.get("item") != index or e.get("kind") not in ("message", "user"):
                continue
            if e.get("kind") == "message" and e["speaker"] == person["id"]:
                push("assistant", e.get("text") or "")
            elif not e.get("pass"):
                push("user", self._render_entry(e))
        spoken = any(e.get("item") == index and e.get("kind") in ("message", "user") for e in transcript)
        ask = [f"It is your turn, {person['name']}. Agenda item {index + 1} of {len(items)}: {items[index]['title']}."]
        if items[index].get("notes"):
            ask.append(f"Notes for this item: {items[index]['notes']}")
        if not spoken:
            ask.append("Open the discussion on this item with your initial view.")
        elif self.d["mode"] == "parallel" and rnd:
            ask.append("Respond to what the others said in the previous round.")
        if self.d["mode"] == "parallel":
            ask.append("Everyone answers at the same time, independently, so give your own answer.")
        if fresh:
            ask.append("The user just spoke (see above). Respond to them directly first.")
        if forced:
            ask.append("The user asked you specifically to speak.")
        text = " ".join(ask)
        if history and history[-1]["role"] == "user":
            text = history.pop()["content"] + "\n\n" + text
        if history and history[0]["role"] != "user":
            history.insert(0, {"role": "user", "content": "(The discussion begins.)"})
        return history, text

    # ----- summaries and minutes

    def _complete(self, person, system, prompt, who="Moderator"):
        """One plain model call by the moderator (or a given participant), no tools."""
        from .llm import LLM
        base = self.state.jarvis.config
        llm = self.d["moderator"] if person is None else person
        overrides = llm_overrides(llm["provider"], llm["model"], base) if llm.get("provider") else (
            {"model": llm["model"]} if llm.get("model") else None)
        cfg = apply_profile_config(base, AgentProfile(llm=overrides))
        self._emit("discussion_speaker", participant="moderator", message_id="", who=who, item=self.d["current_item"])
        try:
            reply = LLM(cfg).chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}])
        finally:
            self._emit("discussion_speaker", participant="", message_id="", item=self.d["current_item"])
        return (reply.get("content") or "").strip()

    def _finish_item(self, index):
        """Write the item summary (decisions, open questions) unless it has none to write."""
        items = self.d["agenda"]["items"]
        if items[index].get("summary"):
            return
        body = self._render_item(index)
        if not body:
            return
        judge = next((p for p in self._people() if p["stance"] == "judge"), None) if self.d["mode"] == "debate" else None
        prompt = (f"Goal of the discussion: {self.d['agenda']['goal']}\nAgenda item {index + 1}: {items[index]['title']}\n\n"
                  f"Transcript of this item:\n{body}\n\n"
                  "Summarize this item in under 150 words with these bold labels: **Outcome**, **Decisions** "
                  "(or 'none'), **Open questions** (or 'none'). Mention who held which view when they disagreed.")
        who = judge["name"] if judge else "Moderator"
        try:
            summary = self._complete(judge, SUMMARY_SYSTEM, prompt, who=who)
        except Exception as error:
            self._note(f"Could not write the summary of item {index + 1}: {_short(str(error), 200)}", level="error")
            summary = "(No summary: " + _short(str(error), 120) + ")\n\n" + _short(body, 600)
        with self.lock:
            items[index]["summary"] = summary
        self._save()
        self._add({"kind": "summary", "speaker": judge["id"] if judge else "moderator", "name": who,
                   "text": summary, "item": index})
        self._emit("discussion_item", item=index, summary=summary, current_item=self.d["current_item"])

    def _write_minutes(self):
        d = self.d
        items = d["agenda"]["items"]
        sections = "\n\n".join(f"### {i + 1}. {it['title']}\n{it.get('summary') or '(not discussed)'}" for i, it in enumerate(items))
        participants = ", ".join(p["name"] for p in d["participants"])
        prompt = (f"Discussion: {d['title']}\nGoal: {d['agenda']['goal']}\nExpected outcome: {OUTCOMES.get(d['agenda']['outcome'])}\n"
                  f"Participants: {participants}, and the user.\n\nItem summaries:\n{sections}\n\n"
                  "Write the minutes as Markdown with exactly these sections: '## Summary' (3 sentences), "
                  "'## Outcome per item', '## Decisions', '## Action items' (a list: '- **Owner**: task'; use "
                  "participant names or 'User'), '## Open questions'. Do not invent anything absent above.")
        try:
            minutes = self._complete(None, SUMMARY_SYSTEM, prompt)
        except Exception as error:
            self._note(f"Could not write the minutes with the model ({_short(str(error), 160)}); "
                       "compiled them from the item summaries instead.", level="error")
            minutes = (f"## Summary\n{d['agenda']['goal']}\n\n## Outcome per item\n{sections}\n\n"
                       "## Decisions\n(see item summaries)\n\n## Action items\n(none recorded)\n\n## Open questions\n(see item summaries)")
        with self.lock:
            d["minutes"] = minutes
        self._save()
        self._add({"kind": "minutes", "speaker": "moderator", "name": "Moderator", "text": minutes, "item": len(items)})
        self._emit("discussion_minutes", minutes=minutes)


# ---------------------------------------------------------------- manager

class DiscussionManager:
    """Everything the routes need. Created lazily by WebState.discussions."""

    def __init__(self, state):
        self.state = state
        self.store = DiscussionStore(mind_dir(state.jarvis.config) / "discussions")
        self.lock = threading.RLock()
        self.engines = {}
        self._docs = {}  # id -> the one live discussion dict (engine and routes share it)
        self._recover()

    def _recover(self):
        """After a restart nothing is running: running/waiting discussions become paused."""
        for discussion_id in self.store.ids():
            d = self.store.load(discussion_id)
            if d and d.get("status") in ("running", "waiting"):
                d["status"] = "paused"
                self.store.save(d)

    # ----- queries

    def summary(self, d):
        items = d["agenda"]["items"]
        return {"id": d["id"], "title": d["title"], "status": d["status"], "mode": d["mode"],
                "goal": d["agenda"]["goal"], "outcome": d["agenda"]["outcome"],
                "participants": [{"id": p["id"], "name": p["name"], "color": p["color"]} for p in d["participants"]],
                "items_total": len(items),
                "items_done": min(d["current_item"], len(items)) if d["status"] != "draft" else 0,
                "created": d.get("created", ""), "updated": d.get("updated", ""),
                "has_minutes": bool(d.get("minutes"))}

    def list(self):
        with self.lock:
            out = [self.summary(d) for d in (self._load(i) for i in self.store.ids()) if d]
        return sorted(out, key=lambda s: s.get("updated") or s.get("created") or "", reverse=True)

    def _load(self, discussion_id):
        with self.lock:
            if discussion_id not in self._docs:
                d = self.store.load(discussion_id)
                if d is None:
                    return None
                self._docs[discussion_id] = d
            return self._docs[discussion_id]

    def get(self, discussion_id):
        """A snapshot copy of the discussion (safe to serialize while the engine runs)."""
        with self.lock:
            d = self._load(discussion_id)
            return copy.deepcopy(d) if d else None

    def detail(self, discussion_id):
        """Discussion, transcript and whatever is streaming right now (for a page that just opened)."""
        d = self.get(discussion_id)
        if d is None:
            return None
        engine = self.engines.get(discussion_id)
        live = []
        if engine:
            live = [{"message_id": m, **{k: v for k, v in info.items()}} for m, info in list(engine.live.items())]
        return {"discussion": d, "transcript": self.store.transcript(discussion_id),
                "live": live, "running": bool(engine and engine.alive())}

    def meta(self):
        """Static choices for the create form, plus what the UI needs about this machine."""
        agents = []
        try:
            agents = [{"id": a.get("id"), "name": a.get("name") or a.get("id"), "description": a.get("description", "")}
                      for a in self.state.agents.list()]
        except Exception:
            pass
        llm = self.state.jarvis.config["llm"]
        return {"modes": [{"id": k, "label": v[0], "help": v[1]} for k, v in MODES.items()],
                "outcomes": [{"id": k, "label": v} for k, v in OUTCOMES.items()],
                "presets": PRESET_ROLES, "templates": TEMPLATES, "colors": COLORS, "agents": agents,
                "limits": {"participants": [MIN_PARTICIPANTS, MAX_PARTICIPANTS], "items": MAX_ITEMS, "rounds": MAX_ROUNDS},
                "current": {"provider": llm.get("provider", ""), "model": llm.get("model", "")}}

    # ----- create / edit

    def create(self, spec):
        fields, warnings = validate_spec(spec, self.state)
        d = {"id": new_id("d"), "status": "draft", **fields, "current_item": 0, "minutes": "",
             "created": now_iso(), "updated": now_iso()}
        self.store.save(d)
        return self.get(d["id"]), warnings

    def patch(self, discussion_id, spec):
        d = self._need(discussion_id)
        engine = self.engines.get(discussion_id)
        if engine and engine.alive() and d["status"] in ("running", "waiting"):
            if set(spec) - {"title"}:
                raise ValueError("Pause the discussion before changing its agenda or participants.")
        merged = {"title": d["title"], "agenda": d["agenda"], "mode": d["mode"], "rounds_per_item": d["rounds_per_item"],
                  "participants": d["participants"], "moderator": d["moderator"], **spec}
        if "agenda" in spec:
            merged["agenda"] = {**d["agenda"], **spec["agenda"]}
        fields, warnings = validate_spec(merged, self.state, previous=d)
        if len(fields["agenda"]["items"]) < min(d["current_item"], len(d["agenda"]["items"])):
            raise ValueError("Items that were already discussed cannot be removed.")
        for old, new in zip(d["agenda"]["items"], fields["agenda"]["items"]):  # keep written summaries
            new["summary"] = new["summary"] or old.get("summary", "")
        with self.lock:
            d.update(fields)
            d["updated"] = now_iso()
            self.store.save(d)
        self._announce(d)
        return self.get(discussion_id), warnings

    def add_item(self, discussion_id, title, notes=""):
        """Continue a finished (or paused) discussion with one more agenda item."""
        d = self._need(discussion_id)
        title = _clean_text(title, 300)
        if not title:
            raise ValueError("The new agenda item needs a title.")
        if len(d["agenda"]["items"]) >= MAX_ITEMS:
            raise ValueError(f"At most {MAX_ITEMS} agenda items.")
        with self.lock:
            d["agenda"]["items"].append({"title": title, "notes": _clean_text(notes, 1000), "summary": ""})
            if d["status"] == "finished":
                d["status"] = "paused"
            d["minutes"] = ""
            d["updated"] = now_iso()
            self.store.save(d)
        self._announce(d)
        return self.get(discussion_id)

    def delete(self, discussion_id):
        self._need(discussion_id)
        engine = self.engines.pop(discussion_id, None)
        if engine:
            engine.stop()
        self._docs.pop(discussion_id, None)
        self.store.delete(discussion_id)

    # ----- control

    def _need(self, discussion_id):
        """The live discussion dict (not a copy); KeyError when unknown."""
        d = self._load(discussion_id)
        if d is None:
            raise KeyError(discussion_id)
        return d

    def _engine(self, discussion_id):
        with self.lock:
            engine = self.engines.get(discussion_id)
            d = self._need(discussion_id)
            if engine is None:
                engine = self.engines[discussion_id] = Engine(self, d)
            return engine

    def _announce(self, d):
        self.state.jarvis.events.emit(Event("discussion_status", d["id"], data={
            "discussion_id": d["id"], "status": d["status"], "current_item": d["current_item"], "updated": True}))

    def control(self, discussion_id, action):
        """start | pause | resume | next | conclude | stop. Raises ValueError for an invalid action now."""
        d = self._need(discussion_id)
        engine = self._engine(discussion_id)
        status = d["status"]
        if action == "start":
            if status != "draft":
                raise ValueError("This discussion has already started.")
            engine.start()
        elif action == "pause":
            if status not in ("running", "waiting"):
                raise ValueError("Only a running discussion can be paused.")
            engine.pause()
        elif action == "resume":
            if status != "paused":
                raise ValueError("Only a paused discussion can be resumed.")
            engine.resume()
        elif action == "next":
            if status not in ("running", "waiting", "paused") or d["current_item"] >= len(d["agenda"]["items"]):
                raise ValueError("There is no item to skip.")
            engine.next_item()
        elif action == "conclude":
            if status == "draft" or (status == "finished" and d.get("minutes")):
                raise ValueError("Nothing to conclude.")
            engine.conclude()
        elif action == "stop":
            if status not in ("running", "waiting", "paused"):
                raise ValueError("The discussion is not active.")
            if engine.alive():
                engine.stop()
            else:  # paused after a restart: no thread to stop
                engine.stopped = True
                engine._note("Stopped by you. The discussion is over; use Conclude to write the minutes.")
                engine._set_status("finished")
        else:
            raise ValueError(f"Unknown action: {action}")
        return self.get(discussion_id)

    def message(self, discussion_id, text):
        """A message from the user: joins the transcript; starts a draft; wakes a waiting engine."""
        text = (text or "").strip()
        if not text:
            raise ValueError("Empty message")
        d = self._need(discussion_id)
        if d["status"] == "finished":
            raise ValueError("This discussion is finished. Use Continue to add an agenda item first.")
        engine = self._engine(discussion_id)
        entry = engine._add({"kind": "user", "speaker": "user", "name": "You", "text": text,
                             "opening": d["status"] == "draft"})
        engine.user_message(entry)
        if d["status"] == "draft":
            engine.start()
        return entry

    # ----- export

    def export_markdown(self, discussion_id):
        d = self._need(discussion_id)
        transcript = self.store.transcript(discussion_id)
        lines = [f"# {d['title']}", "", f"- Status: {d['status']}", f"- Mode: {MODES[d['mode']][0]}",
                 f"- Created: {d.get('created', '')}", "", "## Goal", "", d["agenda"]["goal"], "", "## Agenda", ""]
        for i, item in enumerate(d["agenda"]["items"], 1):
            lines.append(f"{i}. {item['title']}" + (f" — {item['notes']}" if item.get("notes") else ""))
        lines += ["", "## Participants", ""]
        for p in d["participants"]:
            model = f"{provider_label(p['provider'])} · {p['model']}" if p["provider"] or p["model"] else "current model"
            lines.append(f"- **{p['name']}** ({model})" + (f" [{p['stance']}]" if p["stance"] else "") + f": {_short(p['role'], 200)}")
        if d.get("minutes"):
            lines += ["", "## Minutes", "", d["minutes"]]
        lines += ["", "## Transcript"]
        current = None
        for e in transcript:
            if e.get("kind") == "minutes":
                continue
            if e.get("item") != current and e.get("kind") != "system":
                current = e.get("item")
                if isinstance(current, int) and current < len(d["agenda"]["items"]):
                    lines += ["", f"### Item {current + 1}: {d['agenda']['items'][current]['title']}"]
            if e.get("kind") == "system":
                lines += ["", f"_{e['text']}_"]
            elif e.get("kind") == "summary":
                lines += ["", f"> **Summary:** {e['text']}".replace("\n", "\n> ")]
            else:
                tag = " (interrupted)" if e.get("interrupted") else " (pass)" if e.get("pass") else ""
                lines += ["", f"**{e.get('name', '?')}**{tag}:", "", e.get("text", "")]
        return redact_text("\n".join(lines) + "\n")
