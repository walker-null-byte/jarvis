"""Discussions: validation, store, engine modes, failures, user messages, controls, export.

The engine runs against fake Jarvis instances (no network): `ask` returns scripted replies and
records the history it was given, so the tests can check what each speaker saw.
"""

import copy
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis.config import DEFAULTS
from jarvis.discussions import (DiscussionManager, Engine, _is_pass, _json_object,
                                validate_spec)
from jarvis.events import EventBus


class FakeJarvis:
    """Stands in for Jarvis: scripted replies, records what it was asked."""

    script = {}      # name -> list of replies (or exceptions); the last one repeats
    gates = {}       # name -> threading.Event the ask waits on
    calls = []       # (name, history, text) in call order
    lock = threading.Lock()

    def __init__(self, profile):
        self.profile = profile
        self.session_id = f"s_{id(self)}"
        self.history = []
        self.busy = False
        self.cancelled = threading.Event()
        self.config = copy.deepcopy(DEFAULTS)
        self.llm = SimpleNamespace(model="fake-model")
        self.exposure = SimpleNamespace(refresh=lambda: None)

    @property
    def name(self):
        return self.profile.title.rsplit(" — ", 1)[-1]

    def cancel(self):
        self.cancelled.set()

    def ask(self, text, on_text=None):
        name = self.name
        with FakeJarvis.lock:
            FakeJarvis.calls.append((name, list(self.history), text))
            replies = FakeJarvis.script.get(name, ["{} says something".format(name)])
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
        self.busy = True
        try:
            gate = FakeJarvis.gates.get(name)
            if gate:
                while not gate.wait(0.02):
                    if self.cancelled.is_set():
                        self.cancelled.clear()
                        return "Cancelled."
            if isinstance(reply, Exception):
                raise reply
            for word in reply.split(" "):
                on_text(word + " ")
            return reply
        finally:
            self.busy = False


class FakeRuntimes:
    def __init__(self):
        self.closed = []

    def create(self, profile):
        return FakeJarvis(profile)

    def close(self, sid):
        self.closed.append(sid)


def make_state(tmp_path):
    config = copy.deepcopy(DEFAULTS)
    config["paths"]["mind"] = str(tmp_path / "mind")
    jarvis = SimpleNamespace(config=config, events=EventBus(),
                             registry=SimpleNamespace(tool_names=lambda: ["run_command", "web_search", "recall"]))
    return SimpleNamespace(jarvis=jarvis, runtimes=FakeRuntimes(), chatgpt_status=lambda: {"signed_in": False},
                           agents=SimpleNamespace(list=lambda: [], profile=lambda i: None))


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeJarvis.script, FakeJarvis.gates, FakeJarvis.calls = {}, {}, []


@pytest.fixture
def manager(tmp_path, monkeypatch):
    state = make_state(tmp_path)
    mgr = DiscussionManager(state)

    def fake_complete(self, person, system, prompt, who="Moderator"):
        if "moderate" in system:
            return self_script.get("moderator", ['{"next": "none", "advance": true}']).pop(0) \
                if len(self_script.get("moderator", [])) > 1 else (self_script.get("moderator") or ['{"next":"none","advance":true}'])[0]
        if "minutes" in prompt:
            return "## Summary\nDone.\n\n## Decisions\n- ship it\n\n## Action items\n- **A**: do it\n\n## Open questions\nnone"
        return "**Outcome** agreed. **Decisions** none. **Open questions** none."

    self_script = {}
    mgr.script = self_script
    monkeypatch.setattr(Engine, "_complete", fake_complete)
    monkeypatch.setattr(Engine, "_instance", _fake_instance)
    return mgr


def _fake_instance(self, person):
    jarvis = self.instances.get(person["id"])
    if jarvis is None:
        jarvis = FakeJarvis(self._profile(person))
        self.instances[person["id"]] = jarvis
        self.sid_map[jarvis.session_id] = person["id"]
    return jarvis


def spec(**over):
    base = {"title": "T", "agenda": {"goal": "Pick a plan", "items": ["First", "Second"], "outcome": "plan"},
            "mode": "round_robin", "rounds_per_item": 1,
            "participants": [{"name": "Ann", "role": "Researcher", "provider": "lmstudio", "model": "gemma"},
                             {"name": "Bob", "role": "Skeptic", "provider": "lmstudio", "model": "gemma"}]}
    base.update(over)
    return base


def wait_for(manager, discussion_id, status, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if manager.get(discussion_id)["status"] == status:
            return manager.get(discussion_id)
        time.sleep(0.02)
    raise AssertionError(f"status {manager.get(discussion_id)['status']!r}, wanted {status!r}")


def wait_until(check, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met")


def run(manager, **over):
    d, _ = manager.create(spec(**over))
    manager.control(d["id"], "start")
    return d["id"]


def texts(manager, did, kind="message"):
    return [(e["name"], e["text"]) for e in manager.store.transcript(did) if e["kind"] == kind]


# ---------------------------------------------------------------- validation

def test_validation_requires_goal_items_and_participants(tmp_path):
    state = make_state(tmp_path)
    with pytest.raises(ValueError) as error:
        validate_spec({"agenda": {"goal": "", "items": []}, "participants": [{"name": "A"}]}, state)
    message = str(error.value)
    assert "goal is required" in message and "at least one agenda item" in message and "2 to 6 participants" in message


def test_validation_rejects_duplicates_unknown_provider_and_bad_mode(tmp_path):
    state = make_state(tmp_path)
    bad = spec(mode="chaos", participants=[{"name": "A", "provider": "nope"}, {"name": "a"}])
    with pytest.raises(ValueError) as error:
        validate_spec(bad, state)
    for part in ("unknown provider", "must be unique", "Unknown mode"):
        assert part in str(error.value)


def test_validation_warns_about_missing_key_without_blocking(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    state = make_state(tmp_path)
    fields, warnings = validate_spec(spec(participants=[{"name": "A", "provider": "gemini"}, {"name": "B"}]), state)
    assert fields["participants"][0]["provider"] == "gemini"
    assert any("GEMINI_API_KEY" in w for w in warnings)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert validate_spec(spec(participants=[{"name": "A", "provider": "gemini"}, {"name": "B"}]), state)[1] == []


def test_debate_assigns_stances_and_needs_two_sides(tmp_path):
    state = make_state(tmp_path)
    fields, _ = validate_spec(spec(mode="debate", participants=[{"name": "A"}, {"name": "B"}, {"name": "J", "stance": "judge"}]), state)
    assert [p["stance"] for p in fields["participants"]] == ["for", "against", "judge"]
    with pytest.raises(ValueError, match="for and one against"):
        validate_spec(spec(mode="debate", participants=[{"name": "A", "stance": "for"}, {"name": "B", "stance": "for"}]), state)


def test_helpers():
    assert _json_object('```json\n{"next": "p1"}\n```') == {"next": "p1"}
    assert _json_object('Sure: {"a": 1} ok') == {"a": 1}
    assert _json_object("nope") is None
    assert _is_pass("PASS") and _is_pass(" pass. ") and not _is_pass("I pass on that, because")


# ---------------------------------------------------------------- store + lifecycle

def test_create_persists_and_running_becomes_paused_on_restart(manager, tmp_path):
    d, _ = manager.create(spec())
    assert d["status"] == "draft" and d["participants"][0]["color"]
    stored = manager.store.load(d["id"])
    stored["status"] = "running"
    manager.store.save(stored)
    again = DiscussionManager(manager.state)
    assert again.get(d["id"])["status"] == "paused"
    assert [s["id"] for s in again.list()] == [d["id"]]


# ---------------------------------------------------------------- modes

def test_round_robin_runs_every_item_with_summaries_and_minutes(manager):
    FakeJarvis.script = {"Ann": ["Ann point one", "Ann point two"], "Bob": ["Bob reply one", "Bob reply two"]}
    did = run(manager, rounds_per_item=1)
    d = wait_for(manager, did, "finished")
    assert [i["summary"] for i in d["agenda"]["items"]] == ["**Outcome** agreed. **Decisions** none. **Open questions** none."] * 2
    assert d["minutes"].startswith("## Summary") and d["current_item"] == 2
    assert [t for _, t in texts(manager, did)] == ["Ann point one", "Bob reply one", "Ann point two", "Bob reply two"]
    # Bob saw Ann in the transcript as a user message; item 2 saw item 1 only as a summary.
    bob_first = next(c for c in FakeJarvis.calls if c[0] == "Bob")
    assert "Ann: Ann point one" in (bob_first[1][-1]["content"] if bob_first[1] else bob_first[2])
    ann_second = [c for c in FakeJarvis.calls if c[0] == "Ann"][1]
    flat = " ".join(m["content"] for m in ann_second[1]) + ann_second[2]
    assert "[Summary of agenda item 1" in flat and "Ann point one" not in flat
    kinds = [e["kind"] for e in manager.store.transcript(did)]
    assert kinds.count("summary") == 2 and kinds[-1] == "minutes"
    assert manager.state.runtimes.closed  # participant sessions are closed at the end


def test_history_alternates_roles_and_own_turns_are_assistant(manager):
    did = run(manager, rounds_per_item=2, agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "finished")
    ann_second_turn = [c for c in FakeJarvis.calls if c[0] == "Ann"][1]
    roles = [m["role"] for m in ann_second_turn[1]]
    assert roles == ["user", "assistant"] and ann_second_turn[1][1]["content"] == "Ann says something"
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_pass_round_ends_item_early(manager):
    FakeJarvis.script = {"Ann": ["PASS"], "Bob": ["PASS."]}
    did = run(manager, rounds_per_item=3, agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "finished")
    assert len([c for c in FakeJarvis.calls]) == 2


def test_parallel_hides_peers_until_the_round_ends(manager):
    FakeJarvis.script = {"Ann": ["A1", "A2"], "Bob": ["B1", "B2"]}
    did = run(manager, mode="parallel", rounds_per_item=2, agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "finished")
    first_round = [c for c in FakeJarvis.calls][:2]
    for name, history, text in first_round:
        assert "A1" not in text + str(history) and "B1" not in text + str(history)
    second = [c for c in FakeJarvis.calls][2:4]
    for name, history, text in second:
        other = "B1" if name == "Ann" else "A1"
        assert other in text + str(history)
    assert [t for _, t in texts(manager, did)] == ["A1", "B1", "A2", "B2"]


def test_moderated_follows_json_then_falls_back_on_garbage(manager):
    manager.script["moderator"] = ['{"next": "p2", "advance": false, "reason": "Bob first"}',
                                   "this is not json",
                                   '{"next": "none", "advance": true, "reason": "covered"}']
    did = run(manager, mode="moderated", rounds_per_item=1, agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "finished")
    spoken = [name for name, _ in texts(manager, did)]
    assert spoken[0] == "Bob" and "Ann" in spoken       # decision 1 honoured; garbage fell back to the least-heard speaker
    notes = [t for _, t in texts(manager, did, "system")]
    assert any("not valid JSON" in t for t in notes)


def test_moderated_can_ask_the_user_and_resumes_on_reply(manager):
    manager.script["moderator"] = ['{"next": "user", "question": "Budget?"}',
                                   '{"next": "p1", "advance": false}',
                                   '{"next": "none", "advance": true, "reason": "done"}']
    did = run(manager, mode="moderated", agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "waiting")
    assert ("Moderator", "Budget?") in texts(manager, did)
    manager.message(did, "10k")
    wait_for(manager, did, "finished")
    assert any("10k" in c[2] + str(c[1]) for c in FakeJarvis.calls)


def test_debate_alternates_sides_and_judge_writes_the_summary(manager, monkeypatch):
    judged = []
    original = Engine._complete

    def spy(self, person, system, prompt, who="Moderator"):
        if "Summarize this item" in prompt:
            judged.append(who)
        return original(self, person, system, prompt, who)

    monkeypatch.setattr(Engine, "_complete", spy)
    did = run(manager, mode="debate", rounds_per_item=1, agenda={"goal": "g", "items": ["Only"]},
              participants=[{"name": "Ann"}, {"name": "Bob"}, {"name": "Jo", "stance": "judge"}])
    wait_for(manager, did, "finished")
    assert [n for n, _ in texts(manager, did)] == ["Ann", "Bob"]
    assert judged == ["Jo"]
    ann_prompt = FakeJarvis.calls[0]
    assert ann_prompt[0] == "Ann"


# ---------------------------------------------------------------- failures

def test_one_failing_participant_does_not_stop_the_discussion(manager):
    FakeJarvis.script = {"Bob": [RuntimeError("401 bad key")]}
    did = run(manager, rounds_per_item=2, agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "finished")
    notes = [t for _, t in texts(manager, did, "system")]
    assert any("(Bob) failed: 401 bad key" in t for t in notes)
    assert len([n for n, _ in texts(manager, did) if n == "Ann"]) == 2


def test_all_participants_failing_pauses_with_a_clear_note(manager):
    FakeJarvis.script = {"Ann": [RuntimeError("quota")], "Bob": [RuntimeError("quota")]}
    did = run(manager, rounds_per_item=4, agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "paused")
    notes = [t for _, t in texts(manager, did, "system")]
    assert any("Every participant has failed" in t for t in notes)
    manager.control(did, "stop")


# ---------------------------------------------------------------- user participation and controls

def test_first_message_starts_a_draft_and_is_answered(manager):
    d, _ = manager.create(spec(agenda={"goal": "g", "items": ["Only"]}))
    manager.message(d["id"], "Let's begin with pricing")
    wait_for(manager, d["id"], "finished")
    first = FakeJarvis.calls[0]
    assert "Let's begin with pricing" in first[2] + str(first[1]) and "respond to them directly" in first[2].lower().replace("user just spoke (see above). ", "")
    assert manager.store.transcript(d["id"])[0]["opening"] is True


def test_user_message_mid_run_is_addressed_by_the_next_speaker_and_mentions_go_first(manager):
    FakeJarvis.gates = {"Ann": threading.Event()}
    did = run(manager, rounds_per_item=1, agenda={"goal": "g", "items": ["Only"]},
              participants=[{"name": "Ann"}, {"name": "Bob"}, {"name": "Cy"}])
    wait_until(lambda: any(c[0] == "Ann" for c in FakeJarvis.calls))
    manager.message(did, "what about cost? @Cy first please")
    FakeJarvis.gates["Ann"].set()
    wait_for(manager, did, "finished")
    order = [n for n, _ in texts(manager, did)]
    assert order[0] == "Ann" and order[1] == "Cy"      # @mention jumped the queue
    cy = next(c for c in FakeJarvis.calls if c[0] == "Cy")
    assert "what about cost?" in cy[2] + str(cy[1])
    assert "user just spoke" in cy[2]


def test_pause_resume_and_stop(manager):
    FakeJarvis.gates = {"Bob": threading.Event()}
    did = run(manager, rounds_per_item=2, agenda={"goal": "g", "items": ["Only"]})
    wait_until(lambda: any(c[0] == "Bob" for c in FakeJarvis.calls))
    manager.control(did, "pause")
    assert manager.get(did)["status"] == "paused"
    FakeJarvis.gates["Bob"].set()                       # the current speaker finishes, then it holds
    time.sleep(0.3)
    count = len(FakeJarvis.calls)
    time.sleep(0.3)
    assert len(FakeJarvis.calls) == count
    manager.control(did, "resume")
    wait_for(manager, did, "finished")
    assert len([c for c in FakeJarvis.calls if c[0] == "Ann"]) == 2

    did2 = run(manager, agenda={"goal": "g", "items": ["Only"]})
    manager.control(did2, "stop")
    final = wait_for(manager, did2, "finished")
    assert final["minutes"] == "" or final["minutes"]  # stop never crashes
    with pytest.raises(ValueError):
        manager.control(did2, "pause")


def test_stop_cancels_the_speaker_and_keeps_partial_nothing(manager):
    FakeJarvis.gates = {"Ann": threading.Event()}
    did = run(manager, agenda={"goal": "g", "items": ["Only"]})
    wait_until(lambda: any(c[0] == "Ann" for c in FakeJarvis.calls))
    manager.control(did, "stop")
    d = wait_for(manager, did, "finished")
    assert d["minutes"] == ""
    assert any("Stopped by you" in t for _, t in texts(manager, did, "system"))


def test_next_item_skips_ahead_and_conclude_writes_minutes(manager):
    FakeJarvis.gates = {"Bob": threading.Event()}
    did = run(manager, rounds_per_item=3, agenda={"goal": "g", "items": ["One", "Two", "Three"]})
    wait_until(lambda: any(c[0] == "Bob" for c in FakeJarvis.calls))
    manager.control(did, "next")
    wait_until(lambda: manager.get(did)["current_item"] == 1)
    manager.control(did, "conclude")
    d = wait_for(manager, did, "finished")
    assert d["minutes"] and d["current_item"] == 3
    assert d["agenda"]["items"][0]["summary"]


def test_continue_adds_an_item_to_a_finished_discussion(manager):
    did = run(manager, agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "finished")
    d = manager.add_item(did, "Follow-up")
    assert d["status"] == "paused" and len(d["agenda"]["items"]) == 2 and d["minutes"] == ""
    manager.control(did, "resume")
    final = wait_for(manager, did, "finished")
    assert final["agenda"]["items"][1]["summary"] and final["minutes"]


def test_patch_rules(manager):
    d, _ = manager.create(spec())
    updated, _ = manager.patch(d["id"], {"title": "New", "rounds_per_item": 3})
    assert updated["title"] == "New" and updated["rounds_per_item"] == 3
    with pytest.raises(ValueError, match="goal is required"):
        manager.patch(d["id"], {"agenda": {"goal": ""}})


def test_participant_profile_is_research_only_without_persistence(manager):
    d, _ = manager.create(spec(participants=[{"name": "Ann", "tools": True}, {"name": "Bob", "tools": False}]))
    engine = manager._engine(d["id"])
    ann, bob = (engine._profile(p) for p in d["participants"])
    assert ann.kind == "discussion" and ann.owner == d["id"] and not ann.persist and ann.replace_persona
    assert ann.hidden_tools == {"run_command"} and bob.hidden_tools == {"run_command", "web_search", "recall"}
    assert ann.max_tool_rounds == 8 and not ann.allow_spawn


def test_export_markdown_has_minutes_and_transcript(manager):
    did = run(manager, agenda={"goal": "g", "items": ["Only"]})
    wait_for(manager, did, "finished")
    md = manager.export_markdown(did)
    assert md.startswith("# T") and "## Minutes" in md and "## Transcript" in md
    assert "**Ann**" in md and "### Item 1: Only" in md


def test_routes_roundtrip(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvis.webui.server import create_app

    state = make_state(tmp_path)
    web_state = None

    class Shim:
        """create_app wants a Jarvis; hand it the pieces the discussion routes touch."""

    from jarvis.agent import Jarvis
    config = copy.deepcopy(DEFAULTS)
    config["paths"]["mind"] = str(tmp_path / "mind")
    config["voice"]["enabled"] = False
    app = create_app(Jarvis(config), "tok")
    client = TestClient(app, headers={"Authorization": "Bearer tok"}, base_url="http://127.0.0.1:7777")
    assert client.get("/api/discussions").json() == {"discussions": []}
    bad = client.post("/api/discussions", json={"title": "x"})
    assert bad.status_code == 400 and "goal is required" in bad.json()["error"]
    meta = client.get("/api/discussions/meta").json()
    assert {m["id"] for m in meta["modes"]} == {"round_robin", "parallel", "moderated", "debate"}
    created = client.post("/api/discussions", json=spec()).json()
    did = created["discussion"]["id"]
    assert client.get(f"/api/discussions/{did}").json()["discussion"]["status"] == "draft"
    assert client.post(f"/api/discussions/{did}/pause").status_code == 409
    assert client.post("/api/discussions/nope/start").status_code == 404
    assert client.get(f"/api/discussions/{did}/export.md").headers["content-disposition"].endswith('.md"')
    assert client.delete(f"/api/discussions/{did}").json() == {"deleted": True}
