# JARVIS — a lightweight local-first terminal AI assistant

Jarvis is a lightweight replacement for Hermes-style terminal AI workflows: a
local-first personal AI agent with memory, voice, tool calling, artifacts, and
a polished Textual terminal UI. It is an independent project, not affiliated
with Hermes, OpenAI, Google, LM Studio, Ollama, or llama.cpp; the comparison is
about the use case and workflow.

For people looking for a lightweight Hermes alternative, a private terminal
AI assistant, or an OpenAI-compatible local agent, Jarvis keeps the core loop
small and inspectable while still supporting practical desktop workflows.

## Features

- **Core Loop**: LLM-driven agent with streaming, tool calling, and history summarization
- **Desktop Workspace**: Two-pane Textual TUI with a reactive circular waveform, visible composer, sessions, collapsible activity, and diagnostics
- **Memory System**: Episodic → Semantic → Procedural consolidation with
  provenance, review, expiry, and privacy rules
- **Sessions**: Durable JSONL conversations with UI search, resume, Markdown/JSON
  export, new-session, and retry-last-turn actions
- **Artifacts**: Images, charts, tables, Markdown, and JSON shown inline or in a controlled Python window
- **Voice**: Optional Piper TTS + faster-whisper STT with worker-based push-to-talk
- **Tools**: Built-in (bash, files, web) + learned tools hot-loaded from `tools/`
- **MCP Server**: Exposes all tools via Model Context Protocol (stdio)
- **Provider Flexibility**: OpenAI-compatible endpoints for LM Studio, Ollama,
  llama.cpp, OpenAI, Google Gemini, or a custom local/server endpoint
- **Slash Commands**: Change the model, provider, runtime settings, and voice
  behavior without restarting the agent
- **Cron Delegation**: Schedule natural-language jobs (`cron_add`)
- **Supervisor**: Auto-restart on crash with crash-episode logging
- **Self-Improvement**: Skills → Tools pipeline with git-committed changes

## Provider and model support

Jarvis uses the OpenAI-compatible chat-completions interface. The built-in
presets point to real integrations that Jarvis can use today:

- **LM Studio** for local models at `localhost:1234`
- **Ollama** through its OpenAI-compatible endpoint at `localhost:11434`
- **llama.cpp** server at `localhost:8080`
- **OpenAI API** with `OPENAI_API_KEY`
- **Google Gemini API** through Google's OpenAI-compatible endpoint with `GEMINI_API_KEY`

Any reachable OpenAI-compatible endpoint can also be configured as `custom`.
Jarvis does not bundle or imply access to a provider account, model, or API
key; configure the provider and credentials you actually use.

## Quick Start

```bash
# Install
pip install -e .

# Run CLI
jarvis --cli

# Run TUI (with waveform)
jarvis

# Run supervisor (auto-restart)
jarvis-supervisor --cli
```

## Configuration

Edit `config.toml`:

```toml
[llm]
provider = "lmstudio"
base_url = "http://localhost:1234/v1"  # LM Studio
model = "gemma4"
api_key_env = "JARVIS_API_KEY"

[voice]
enabled = true
engine = "piper"
model = "en_US-lessac-medium"
stt_model = "small"
```

## Slash commands

The same commands work in the TUI composer and in `jarvis --cli`:

```text
/help
/model [NAME]
/model list
/provider
/provider list
/provider use ollama
/provider set http://localhost:8000/v1 KEY_OR_ENV
/provider test [preset]
/provider models [preset]
/settings
/settings set llm.stream off
/settings get llm.timeout
/settings save
/status
```

Changes apply immediately to the running session. Provider, model, and the
other runtime values persist automatically to `mind/runtime_config.json`
when you switch via `/provider` or `/model` (or Apply in the provider
manager); `/settings save` re-saves the same file for the remaining
settings. API secrets themselves are never stored there — they come from
environment variables, or are pasted in as session-local literal keys.

Endpoint QOL: `/provider use` and `/provider set` verify the connection right
away, and `/provider set` auto-appends the missing `/v1` path when the
endpoint rejects the bare URL (e.g. `http://host:6446` becomes
`http://host:6446/v1`). `/provider test` and `/provider models` re-check the
current provider (or a named preset) and list the models it exposes;
`/model list` does the same for the active endpoint. `/status` reports the
API key source and the last LLM error so a broken session is diagnosable at a
glance.

Each provider remembers its own model: switching from `openai` back to your
custom endpoint restores the model that endpoint was last using, instead of
carrying a foreign model over and failing. A literal key pasted via
`/provider set URL oc-…` wins over the env var and is kept only for the
session (the UI masks it in the echo).

In the TUI, `/provider` (bare, or `/provider use`/`/provider set` without
arguments) opens the **provider manager**: pick a preset or type a custom
URL, paste the API key (or an env var name), press Test to verify the
connection and populate the model dropdown from the endpoint itself, then
choose the model and press Apply. While you type the URL or key, the manager
debounces and re-tests automatically so the model list stays in sync (the
previous list is kept if a re-test fails). A missing `/v1` path is fixed for
you: the test retries with it appended and the URL field is updated to the
URL that worked. The test checks the endpoint and lists its models without
insisting that your previously configured model exists there, and the
dropdown prefers the model remembered for the provider you are configuring.
Apply persists the provider and model, so the next launch comes back with
the same setup. The form stays put on small terminals — it never
auto-scrolls itself off-screen. Commands that need values open a guided
form instead of asking you to type arguments from memory: `/model` asks for
a model name and `/settings get/set/reset` let you choose a setting from a
list. The palette shows these entries as before — select one and press
Enter to open the form; complete the fields and press Apply (or Enter) to
run it. Tab only completes the command text.

## Architecture

```
jarvis/
├── agent.py          # Core loop: user → LLM → tools → reply
├── llm.py            # OpenAI-compatible client
├── tools_core.py     # Built-in tools
├── registry.py       # Hot-reload tools/ directory
├── mcp_server.py     # MCP stdio server
├── memory.py         # FactStore + EpisodeLog
├── consolidate.py    # Dream cycle (consolidation)
├── codify.py         # Skills → Tools pipeline
├── voice.py          # TTS abstraction (Piper/Kokoro)
├── listen.py         # STT (faster-whisper)
├── models_manager.py # HF model downloads
├── ui.py             # Textual TUI
├── events.py         # Runtime event bus for UI and future web clients
├── sessions.py       # Durable JSONL sessions
├── artifacts.py      # Artifact tools and persistence
├── surfaces.py       # Controlled detached artifact windows
├── surface_window.py # Small Tk artifact renderer
├── supervisor.py     # Crash recovery wrapper
├── run_job.py        # Headless cron runner
└── jobs.py           # Cron tooling
```

## Memory Layout

```
mind/
├── self.md           # Identity + code map
├── personality.md    # Evolving voice/style
├── user.md           # User facts
├── system.md         # Machine facts
├── MEMORY.md         # Index (always in context)
├── episodes/         # Raw daily logs
├── facts/            # Semantic memories (strength + why)
├── skills/           # Procedural: how-to guides
└── jobs/             # Cron job specs
```

## Cron Jobs

Jobs use a recorded-only private backend by default. Set `[jobs].backend = "system"`
to install entries in the current user's crontab; `cron_status` reports whether
the scheduler is installed, unavailable, or only recorded locally.

```python
# Add a job
cron_add("disk-check", "0 * * * *", "check disk space and alert if > 90%")

# List jobs
cron_list()

# Enable/disable
cron_disable("disk-check")
cron_enable("disk-check")
```

## Development

```bash
# Install dev deps
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .

# Type check
mypy jarvis/
```
