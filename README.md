# JARVIS

A self-improving personal AI agent with human-like memory, voice, and the ability to grow its own toolset.

## Features

- **Core Loop**: LLM-driven agent with streaming, tool calling, and history summarization
- **Memory System**: Episodic → Semantic → Procedural consolidation (dream cycle)
- **Voice**: Piper TTS + faster-whisper STT with waveform visualization
- **Tools**: Built-in (bash, files, web) + learned tools hot-loaded from `tools/`
- **MCP Server**: Exposes all tools via Model Context Protocol (stdio)
- **Cron Delegation**: Schedule natural-language jobs (`cron_add`)
- **Supervisor**: Auto-restart on crash with crash-episode logging
- **Self-Improvement**: Skills → Tools pipeline with git-committed changes

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
base_url = "http://localhost:1234/v1"  # LM Studio
model = "gemma4"

[voice]
enabled = true
engine = "piper"
model = "en_US-lessac-medium"
stt_model = "small"
```

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