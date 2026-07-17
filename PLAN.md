# Jarvis — Implementation Plan

A self-improving personal agent with a human-like memory system, voice, and the
ability to grow its own toolset.

## Design principles

- **Simple, debuggable code.** Small modules, plain functions, no clever
  abstractions. A human should be able to read any file top to bottom.
- **The memory system IS the learning.** No fine-tuning. Files + consolidation
  give permanent learning that is inspectable and revertible.
- **Jarvis can modify `mind/` and `tools/`, never its own core.** Core changes
  are proposed as diffs for the user to approve. Everything Jarvis writes is
  git-committed, so `git log` is its learning history and anything can be
  reverted.

## Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| LLM | `openai` client with configurable `base_url` + `model` (local server first, Gemini's OpenAI-compatible endpoint later) |
| Tool calling | OpenAI function-calling format (supported by Gemini compat + llama.cpp/Ollama) |
| UI | Textual TUI — chat pane + live waveform strip |
| TTS | Piper (default, tiny/fast) or Kokoro; models downloaded from HF or dropped in `models/` |
| STT | faster-whisper (small model), push-to-talk in the TUI |
| MCP | FastMCP server exposing the same tool registry to external clients |
| Versioning | git repo over `mind/` + `tools/` |

## Layout

```
jarvis/
├── config.toml          # base_url, api_key env name, model, voice settings, paths
├── main.py              # the loop: input → context → LLM → tools → output
├── llm.py               # OpenAI-compatible client, streaming, tool-call parsing
├── context.py           # builds the prompt: self.md + personality + user + MEMORY index
│                        # + relevant facts/skills + rolling chat (auto-summarizes old turns)
├── tools_core.py        # built-in tools: bash, read/write file, memory ops, cron ops
├── registry.py          # discovers tools (core + tools/*.py), emits schemas, hot-reload
├── mcp_server.py        # FastMCP server serving the registry to external clients
├── voice.py             # TTS engine abstraction + amplitude feed for the waveform
├── listen.py            # STT (push-to-talk → faster-whisper)
├── models_manager.py    # download TTS/STT models from HF, or accept custom drops in models/
├── ui.py                # Textual app: chat + waveform + status line
├── consolidate.py       # the dream cycle
├── supervisor.py        # relaunch-on-crash wrapper; crash reports become episodes
├── run_job.py           # headless one-turn Jarvis for cron-delegated jobs
├── models/              # downloaded/dropped voice model files
├── mind/                # everything Jarvis knows (git-tracked)
│   ├── self.md          # identity + map of its own code (self-awareness)
│   ├── personality.md   # evolving voice/style
│   ├── user.md          # facts about the user (user-seeded, Jarvis-maintained)
│   ├── system.md        # learned facts about this machine
│   ├── MEMORY.md        # index of all memories, always in context
│   ├── episodes/        # episodic: raw daily logs (conversations, commands, crashes)
│   ├── facts/           # semantic: one fact per file with strength score + why
│   ├── skills/          # procedural: skill.md (how-to) per learned skill
│   └── jobs/            # cron-delegated jobs, one file each (auditable)
└── tools/               # generated Python tools, hot-loaded by registry + MCP
```

## Memory: episodic → semantic → procedural

1. **During the day** Jarvis only appends to `episodes/` (cheap, no judgment
   calls mid-conversation). Tool runs, command outcomes, corrections from the
   user, and crashes all land here.
2. **Dream cycle** (`consolidate.py`, runs on exit + nightly cron) replays
   recent episodes and:
   - distills lessons into `facts/` (each with a strength score and a *why*)
   - **reinforces & decays**: recalled facts strengthen; untouched facts decay
     and are eventually archived; contradicted facts get corrected — spaced
     repetition keeps `mind/` small and true
   - updates `personality.md` and `user.md` when warranted
   - **promotes procedures**: a task type seen repeatedly → drafts
     `skills/<name>.md`; a skill that has worked reliably → codifies
     `tools/<name>.py`, smoke-tests it, git-commits it. The registry hot-loads
     it, so it's instantly a callable tool *and* an MCP tool.
3. **Context budget**: `context.py` tracks rough token counts; when the rolling
   chat grows past a threshold, old turns are summarized into a compact block
   and the full text is archived to `episodes/`.

## Self-improvement guardrails

- Write access: `mind/` and `tools/` only. Core files → propose a diff, ask.
- Every self-modification is one git commit with a message saying what was
  learned and why.
- New tools must pass a smoke test in the dream cycle before being loaded.
- `supervisor.py` relaunches Jarvis on crash and writes a crash episode, so
  failures literally become memories it learns from.

## Voice

- **Out**: `voice.py` exposes `speak(text)` behind an engine interface.
  Default Piper; Kokoro optional. Code blocks are never spoken; for long
  answers the model wraps a 1–2 sentence spoken summary in `<say>…</say>`,
  the rest stays text-only.
- **Fallback chain**: configured TTS → smallest local fallback voice →
  text-only with canned status lines. Works fully offline once models exist.
- **Model management**: `models_manager.py` downloads from Hugging Face on
  first use, or accepts custom model files dropped into `models/`.
  Everything optional via `config.toml` (`voice.enabled`, `voice.engine`, …).
- **In**: push-to-talk key in the TUI → faster-whisper → text into the loop.
- **Waveform**: TTS playback feeds amplitude to the TUI, rendered as a live
  unicode block/braille waveform.

## Cron delegation (general, not just learning)

A built-in tool lets Jarvis create jobs: `cron_add(name, schedule, instruction)`.
- The job spec lands in `mind/jobs/<name>.md` (auditable, deletable).
- A crontab entry runs `python run_job.py <name>` — a headless single-turn
  Jarvis that executes the instruction with the full tool registry and logs the
  outcome to `episodes/`.
- The nightly dream cycle is itself just one of these jobs.

## Build order

0. ✅ **ContextAware overhaul** — JSON Schema, native MCP stdio server (+ lazy
   mode, list_changed), bug fixes, zero-dep packaging, README. Publishable.
1. ✅ **Core loop** — config, `llm.py`, `tools_core.py` (bash + files), TUI
   chat (Textual) + `--cli` mode, episode logging, mind/ scaffolding.
   *Usable assistant — tested live against LM Studio (gemma4).*
2. ✅ **Mind** — FactStore (one fact per file, strength scores, MEMORY.md
   index), remember/recall/forget tools, history summarization between turns.
   *Cross-session memory tested live. Default model: gemma4 (loaded in LM
   Studio). The prompt now explicitly requires `recall` before answering
   questions about the user.*
3. ✅ **Dream cycle** — `consolidate.py` runs on exit and via nightly cron.
   Reads episodes, distills facts with LLM, decays untouched memories,
   corrects contradictions, drafts skill files. Every change git-committed.
   *Tested end-to-end: session → consolidation → new fact file → cross-session
   recall works.*
3. **Dream cycle** — consolidation on exit + cron, strength/decay, skill drafting.
4. **Skills → tools** — codification pipeline, registry hot-reload, MCP server,
   git auto-commit, smoke tests.
5. **Voice** — TTS + model manager + waveform; then STT push-to-talk.
6. **Resilience & jobs** — supervisor, crash-episodes, cron delegation tool.

## Open items

- "contextaware for the MCP" from the original notes — assumed to mean the
  MCP/tool layer should expose system context (OS, paths, running state).
  Correct this if it refers to a specific library.
- Which local model server is running (Ollama? llama.cpp? LM Studio) — only
  affects the default `base_url` in `config.toml`.
