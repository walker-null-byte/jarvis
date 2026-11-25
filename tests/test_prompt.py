"""System prompt: stability across tool rounds, memory cap, skills, persona."""

import re

from jarvis.context import build_system_prompt, ensure_mind
from tests.test_core_tools import ScriptedLLM, _agent, _calls, _config, _tool_call


def test_system_prompt_built_once_per_turn_without_clock(tmp_path):
    llm = ScriptedLLM(
        _calls(_tool_call("list_dir", {"path": "."})),
        _calls(_tool_call("list_dir", {"path": "."}, "c2")),
    )
    jarvis = _agent(tmp_path, llm)
    built = []
    original = jarvis._build_prompt
    jarvis._build_prompt = lambda: built.append(1) or original()
    jarvis.ask("go")
    assert len(built) == 1
    prompts = {r["messages"][0]["content"] for r in llm.requests}
    assert len(llm.requests) == 3 and len(prompts) == 1
    assert not re.search(r"\b\d{1,2}:\d{2}\b", prompts.pop())


def test_budget_wrapup_reuses_system_prompt(tmp_path):
    llm = ScriptedLLM(_calls(_tool_call("list_dir", {"path": "."})))
    jarvis = _agent(tmp_path, llm, max_tool_rounds=1)
    jarvis.ask("go")
    # Wrap-up keeps the identical tools array (APIs reject tool history without
    # tools, and a changed array breaks prompt caching)...
    assert llm.requests[-1]["tools"] == llm.requests[0]["tools"]
    assert llm.requests[0]["messages"][0] == llm.requests[-1]["messages"][0]
    # ...and any tool call made during the wrap-up is dropped, never left dangling.
    assert "tool_calls" not in jarvis.history[-1]


def test_memory_index_is_capped_at_line_boundary(tmp_path):
    config = _config(tmp_path)
    mind = ensure_mind(config)
    (mind / "MEMORY.md").write_text("\n".join(f"- fact number {i}" for i in range(500)))
    prompt = build_system_prompt(mind, memory_chars=200)
    assert "fact number 0" in prompt and "fact number 499" not in prompt
    assert "use recall" in prompt
    assert "fact number 499" in build_system_prompt(mind, memory_chars=100000)


def test_skills_index_lists_titles(tmp_path):
    mind = ensure_mind(_config(tmp_path))
    (mind / "skills" / "backup.md").write_text("# Back up my notes\n## Steps\n- zip\n")
    prompt = build_system_prompt(mind)
    assert "# Skills" in prompt and "- backup: Back up my notes" in prompt
    assert "read_skill" in prompt


def test_persona_block_always_from_active_card(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    (jarvis.mind / "user.md").write_text("# About\n- Name: Dil\n")
    default = jarvis._build_prompt()
    assert "# Persona: Jarvis" in default and "Calm, dry wit" in default
    assert "different persona" not in default
    jarvis.set_character("casual")
    swapped = jarvis._build_prompt()
    assert "# Persona: Jarvis (Casual)" in swapped and "Calm, dry wit" not in swapped
    assert "different persona" in swapped and "Hey Dil" not in swapped
    assert swapped.index("# Who you are") < swapped.index("# Persona:")


def test_edited_jarvis_card_changes_prompt(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    jarvis.store_characters.save({"personality": "Gruff and brief."}, "jarvis")
    prompt = jarvis._build_prompt()
    assert "Gruff and brief." in prompt and "Calm, dry wit" not in prompt


def test_starters_have_preferences_and_no_personality_file(tmp_path):
    mind = ensure_mind(_config(tmp_path))
    assert not (mind / "personality.md").exists()
    assert "## Preferences" in (mind / "user.md").read_text()
    assert "characters/" in (mind / "self.md").read_text()
    prompt = build_system_prompt(mind)
    assert "## Preferences" in prompt
    assert "Preferences\" in user.md" in prompt


def test_existing_user_md_not_overwritten(tmp_path):
    config = _config(tmp_path)
    mind = ensure_mind(config)
    (mind / "user.md").write_text("# mine\n- Name: Zed\n")
    ensure_mind(config)
    assert (mind / "user.md").read_text() == "# mine\n- Name: Zed\n"


def test_personality_migration_custom(tmp_path):
    config = _config(tmp_path)
    mind = ensure_mind(config)
    (mind / "personality.md").write_text("# Personality\nGruff pirate.\n")
    ensure_mind(config)
    assert not (mind / "personality.md").exists()
    assert (mind / "personality.md.migrated").read_text().startswith("# Personality")
    prompt = build_system_prompt(mind)
    assert "Gruff pirate." in prompt and "Calm, dry wit" not in prompt
    # idempotent: a second run (and a re-created file) is handled without error
    ensure_mind(config)
    assert "Gruff pirate." in build_system_prompt(mind)


def test_personality_migration_stock_keeps_builtin(tmp_path):
    from jarvis.context import PERSONALITY_MD
    config = _config(tmp_path)
    mind = ensure_mind(config)
    (mind / "personality.md").write_text(PERSONALITY_MD)
    ensure_mind(config)
    assert (mind / "personality.md.migrated").exists()
    assert not (mind / "characters" / "jarvis.json").exists()
    assert "Calm, dry wit" in build_system_prompt(mind)


def test_tools_section_lists_names_only(tmp_path):
    jarvis = _agent(tmp_path, ScriptedLLM())
    prompt = jarvis._build_prompt()
    assert "# Tools" in prompt and "find_tools" in prompt and "cron_add" in prompt
    assert "input_schema" not in prompt
