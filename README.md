# Preface 

I made Jarvis during the early open models era. My vision(pun intended) was to create a self improving AI harness(that preferable uses local models) that is aware of its code, and can modify itself. I also made my own version of MCP using regex even before Anthropic knew what a reasoning model was.

But i was always very lazy, and due to my college work i had to put my project on hold. Anyhow, since then LLMs have come a long way, and now i can finally focus on the creative part (architecture) and let LLMs write the code for me. I use latest MCP and tech stack to implement Jarvis instead of the primitive code i wrote back in college.

Still, i use my own MCP package - [TurboMCP](https://www.github.com/walker-null-byte/turbomcp)

Feel free to ask for any additions, or improvements, i will try my best to implement them, In Sha Allah. 


# JARVIS — a local, self-improving AI agent

Jarvis is a personal AI agent that runs on your own computer. It has real tools
(shell, files, web, scheduled jobs), a memory that persists between sessions,
skills it learns from experience, swappable personalities, optional voice, and a
web UI. It works with your ChatGPT plan, local models (LM Studio, Ollama,
llama.cpp, vLLM) or any API-key provider.

> **Have ChatGPT Plus or Pro?** You can sign in with your ChatGPT account and
> use your existing plan — no API key and no separate API billing.

---

## Tutorial: up and running in 5 minutes

### 1. Install

```bash
pipx install jarvis-harness          # or: pip install jarvis-harness
```

Your settings and memory live in `~/.jarvis` (set `JARVIS_HOME` to move them),
and file tools can reach your home folder.

Want to hack on Jarvis itself? Run it from a clone instead — then the clone is
its home and file tools stay inside it:

```bash
git clone https://github.com/walker-null-byte/jarvis.git
cd jarvis
python -m venv .venv && source .venv/bin/activate   # fish: source .venv/bin/activate.fish
pip install -e .
```

One install gets everything (web UI, terminal UI, all providers, voice engines,
web tools). Large **models** (voices, speech recognition, embeddings) are never
bundled — you download the ones you want with one click, and the size is shown
first.

### 2. Open the web UI

```bash
jarvis --web
```

Your browser opens a private link (`http://127.0.0.1:7777/?token=…`). The
first launch shows a short **welcome tour**:

1. **Connect a model** — pick one:
   - **Sign in with ChatGPT** — uses your Plus/Pro plan. A browser tab opens,
     you approve, and you're done.
   - **Local model** — start LM Studio or Ollama, pick it, press *Test*.
   - **API key** — OpenAI API, Anthropic, Gemini, OpenRouter, Groq, DeepSeek,
     Mistral, xAI… paste the key (it's remembered on this computer), or set
     its environment variable, e.g. `export ANTHROPIC_API_KEY=…`.
2. **Choose a character** — Jarvis (calm, dry wit), Jarvis Casual, or J-7
   Robotic. You can switch any time.
3. **Quick tips** — and you're chatting.

### 3. Things to try

- *"How much disk space do I have left?"* — Jarvis runs a command and reports.
- *"Remember that I prefer short answers."* — saved to memory for every future
  session.
- *"Every weekday at 9, summarize my ~/notes folder."* — creates a routine.
- **Agents → Lead Finder** — fill in what you offer and who a good lead is,
  then press *Find leads*.
- **Discussions → New discussion** — give a goal and an agenda, pick two or
  three participants (any providers), press *Start*.
- Hover a reply: **Copy**, **Re-do response**, **Speak**. Hover your own
  message: **Edit** and resend.
- Quota ran out or want a different model? Click the **provider · model** pill
  in the header and switch — it applies from the next message.
- Turn on **Auto-speak** in the header to hear every reply (off by default).

Prefer the terminal? `jarvis` opens the terminal UI, `jarvis --cli` a plain
prompt. Both share the same agent, memory and slash commands.

---

## Features

### Connect any model — switch mid-chat

| Provider | How you pay | Setup |
|---|---|---|
| **ChatGPT** | Your ChatGPT Plus/Pro plan | Sign in with ChatGPT (`/login chatgpt`) |
| LM Studio, Ollama, llama.cpp, vLLM | Free, runs locally | Start the server, pick the preset |
| OpenAI API | Pay-as-you-go API key | `OPENAI_API_KEY` |
| Anthropic (Claude) | API key | `ANTHROPIC_API_KEY` |
| Gemini, OpenRouter, Groq, DeepSeek, Mistral, xAI | API key | paste in Settings, or each provider's `*_API_KEY` |
| Cloudflare Workers AI, AI Gateway | Cloudflare API token | `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` ([details](#cloudflare)) |
| Any OpenAI-compatible server | — | `/provider set URL KEY_OR_ENV` |

#### Cloudflare

Both presets need your **Account ID** and an API token with the **Workers AI
Read** permission (AI Gateway permissions alone are rejected). Enter them in
Settings, set the two environment variables, or run
`/provider use cloudflare account_id=YOUR_ID` and provide the token by
environment variable.

- **Workers AI** serves Cloudflare-hosted open models (`@cf/…`).
- **AI Gateway** sends requests through your gateway (logs, caching, limits).
  Leave Gateway ID empty to use `default`, which Cloudflare creates on the first
  request; other gateways must already exist. The model list shows only Workers
  AI models because Cloudflare has no API listing the rest. Type third-party ids
  as `author/model`, such as `openai/gpt-4.1`; these need AI Gateway credits or
  your provider key stored in the gateway.

*ChatGPT* and *OpenAI API* are different on purpose: ChatGPT uses the
subscription you already have; the OpenAI API bills per token. ChatGPT sign-in
is for personal, local use and draws from your plan's usage limits (on Plus,
the 5-hour limit is shared with your other apps). When a limit is hit, Jarvis
tells you and offers to switch providers.

Anthropic's terms don't allow Claude Pro/Max subscriptions in third-party
apps, so Claude is used with an API key. Jarvis uses the official SDK with
prompt caching and adaptive thinking.

### Tools without context bloat (TurboMCP)

Jarvis has dozens of tools, but the model only sees a handful of core ones
(run a command, read/edit/write files, grep, glob, remember/recall, create a
routine, spawn a subagent) plus `find_tools`,
`call_tool` and `read_skill`. Everything else is discovered on demand through
TurboMCP search. That keeps every request small — and identical from round to
round, so prompt caching works on hosted APIs and local servers don't
re-process the whole conversation. Download the small embedding model (31 MB)
for smarter, meaning-based tool search. Set `agent.tool_mode = "all"` for
models that can't use tool search.

### Agents — specialised, importable plugins

An agent is a folder that turns Jarvis into a specialist: a role prompt,
settings you fill once (including API keys, stored privately), action buttons
with their own inputs, tables it fills in (records), exports (CSV, JSON,
Markdown or your own template), optional Python tools and skills, and its own
chat. It can still use every Jarvis tool.

- **Lead Finder** (built in) follows a manual lead-generation workflow: find
  people through public Instagram/LinkedIn profiles, score their fit (quality
  over quantity), look up public contact details with sources (never guessed),
  draft a personal first message, and after a reply draft the follow-up with
  your calendar link. You send the messages yourself — Jarvis never automates
  DMs, which would break those platforms' terms.
- **Import** an agent from a `.zip`, a GitHub link or a folder. Its Python
  code only loads after you review and trust it. **Export** it (optionally
  with its records; never with secrets), **duplicate** a built-in to change it,
  or create a prompt-only agent with *New agent*.
- **Make with AI**: describe the agent in plain words (the buttons it needs,
  what to keep track of, which APIs and keys) and the Agent Builder makes it
  in a chat while a live preview fills in. **Edit with AI** changes an
  installed agent the same way ("add a field for genre") and shows what
  changed before you apply it; records and settings are kept. Every draft is
  strictly checked (manifest, missing libraries, network use, hard-coded
  keys) and can't be installed while anything is wrong; generated Python
  tools can be test-run in a separate process after you approve. Works best
  with a strong model.
- Format reference: [docs/agent-plugins.md](docs/agent-plugins.md).

### Routines — recurring and timed tasks

Ask in plain English (*"every weekday at 9"*, *"every 2 hours"*, *"tomorrow at
8"*) or use cron. A routine can ask Jarvis something or run an agent action
(e.g. a weekly lead hunt). Each run gets its own conversation; you see the
history, a toast, a badge and (if you allow it) a desktop notification.
Routines run while `jarvis --web` is open, or always with `jarvis --daemon`
(see `jarvis.service`). Missed runs can catch up once. Old cron jobs are
imported automatically (paused if your crontab still runs them).

### Discussions — group chats with an agenda

Put 2–6 participants in a room, each on any provider and model (Gemini, a
local model, Claude…), with a mandatory goal and agenda. Modes: **round
robin**, **parallel** (independent answers, revealed together), **moderated**
(a moderator picks who speaks and when to move on) and **debate**. Participants
can research with web search. You can speak at any time or `@mention`
someone; pause, skip to the next item or conclude. Each item gets a summary
and the discussion ends with minutes (decisions, action items, open
questions), exportable as Markdown.

### A stronger harness

- **Parallel tool calls** for read-only tools (reads, searches, web, subagents).
- **Coding tools**: `read_file` by line range, exact-match `edit_file`,
  `grep` (ripgrep), `glob`.
- **Large outputs** are saved to a file; the model gets the start, the end and
  the path, so context stays small.
- **Background commands** for builds and servers (`run_command(background=true)`).
- **MCP servers**: paste the JSON from any MCP server's README (Claude Desktop
  format) in *Tools*. Their tools join the searchable catalog, so 50 MCP tools
  cost almost nothing up front. Read-only tools run freely; others ask once
  per server.
- **Subagents** (`spawn_agent`): hand broad research to a helper with its own
  context and get back only the answer — or delegate to an installed agent.
- Up to 50 tool rounds per message, with a **Continue** button after that.

### Memory that grows

- **Facts** — `remember` / `recall`, with provenance, review, expiry and
  never-remember privacy rules. Review them in the web UI's *Memory* tab.
- **Episodes** — every conversation is logged to `mind/episodes/`.
- **Dream cycle** — consolidation distills episodes into facts and drafts
  skills (*Run dream cycle* in the *Mind* tab, or `jarvis-consolidate`).
- **Preferences** — what Jarvis learns about how you like it to behave lives in
  `mind/user.md` under *Preferences* and applies to every character.

### Self-improvement

Jarvis can turn a repeated procedure into a skill (`mind/skills/*.md`), and a
skill into a real tool (`tools/*.py`, hot-reloaded and git-committed). Skills
are listed in its prompt and read on demand; tools appear in tool search the
moment they're written.

### Characters (hot-swappable personas)

- Built-in: **Jarvis**, **Jarvis (Casual)**, **J-7 (Robotic)** — all editable.
- **Import** SillyTavern character cards (V1/V2/V3, `.json` or `.png`).
- Edit a few simple fields: name, description, personality, greeting, example
  dialogue, instructions, tags. Export as SillyTavern JSON.
- Switch mid-chat from the header or `/character use <id>`. A persona changes
  Jarvis's name, voice and style — never its tools, permissions or safety rules.

### Voice (local, optional, off by default)

Speech runs on your machine and works with every provider.
- **Speak** any reply with one click, or turn on **Auto-speak**.
- Engines: **Piper** (default) or **Kokoro** (optional one-click install).
- Models download on demand from the *Voice* tab or `/voice download <id>`:

  | Model | Size |
  |---|---|
  | Piper voices (lessac, amy, alan) | 63 MB each |
  | Piper ryan (high quality) | 121 MB |
  | Kokoro v1.0 (many voices) | 121 MB + small package install |
  | Whisper speech recognition: tiny / base / small | 78 / 148 / 486 MB |
  | Embeddings for smarter tool search | 31 MB |

### Web UI

A Claude-style sidebar (New chat, Search, Agents, Discussions, Routines,
Tools, Recents). Streaming chat with live thinking, tokens/sec, tool-call
cards, inline approvals for risky commands, artifacts (images, tables,
charts), sessions (search, rename, export), and side panels for Characters,
Mind, Memory, Voice and Settings. It binds to `127.0.0.1` with a per-run secret token; Jarvis can run
commands on your machine, so don't expose it to a network you don't trust
(`--allow-remote` exists, with a warning).

### Safety

`security.mode = "guarded"` (default) blocks destructive command patterns,
asks for approval before risky ones (sudo, redirects, pipes, `git push`…), and
keeps file tools inside `allowed_roots`.

---

## Slash commands

Work in the web UI, the terminal UI and `--cli`:

```text
/help                                       all commands
/provider list | use <name> | set URL KEY   switch providers
/model [NAME] | list                        switch models
/login chatgpt | /logout chatgpt            ChatGPT sign-in
/character list|use|show|import|delete|export|reset
/voice status|on|off|engine|models|download
/settings | get | set | reset | save        runtime settings
/status                                     provider, model, voice
```

## Running it

```bash
jarvis --web [--port 7777] [--no-browser]   # web UI
jarvis                                       # terminal UI
jarvis --cli                                 # plain terminal
jarvis --daemon                              # headless: run routines in the background
jarvis --doctor                              # check endpoint, config and mind
jarvis-supervisor --cli                      # auto-restart on crash
jarvis-mcp                                   # expose the tools as an MCP server (stdio)
```

## Configuration

Settings come from `config.toml` in Jarvis's home (`~/.jarvis`, or the clone;
copy `config.example.toml`), then
`mind/runtime_config.json` (what you change in the UI), then `JARVIS_*`
environment variables. API keys you paste in the UI are saved to
`~/.config/jarvis/keys.json` (owner-only permissions; untick *Remember on this
computer* to keep a key for this session only) and win over environment
variables; the ChatGPT sign-in is stored next to it. MCP servers live in
`mind/mcp.json`.

## Where things live

```
jarvis/                 the agent
├── agent.py            core loop: message → model → tools → reply
├── tool_exposure.py    TurboMCP tool search (find_tools / call_tool)
├── runtime.py          several agents in one process (profiles)
├── file_tools.py       read_file / edit_file / grep / glob
├── mcp_client.py       MCP servers from mind/mcp.json
├── subagents.py        spawn_agent
├── specialists/        Agents: plugin format, records, import/export
├── agents_builtin/     built-in agents (Lead Finder)
├── routines.py         scheduler for recurring tasks
├── discussions.py      group discussions
├── providers/          chat-completions, ChatGPT sign-in, Anthropic
├── characters.py       character cards (import, edit, render)
├── context.py          system prompt built from mind/
├── memory.py           facts + episode log
├── consolidate.py      dream cycle
├── codify.py           skills → tools
├── voice.py, listen.py text-to-speech, speech-to-text
├── models_manager.py   downloadable model catalog
├── webui/              FastAPI server + plain-JS frontend
└── ui.py               terminal UI
mind/                   Jarvis's memory (personal, not committed)
├── self.md  user.md  system.md  MEMORY.md
├── characters/  episodes/  facts/  skills/  sessions/
├── agents/  agent_data/  routines/  discussions/  mcp.json
tools/                  learned tools (hot-reloaded)
models/                 downloaded voice/STT models (not committed)
```

## Development

```bash
pip install -e ".[dev]"
pytest            # hermetic: no network, models or audio devices needed
ruff check .
```

---

## Changelog

### 1.3.0 — 2026-11-26

**New**
- **Agent Builder**: *Make with AI* in the New agent dialog and *Edit with AI*
  on every agent. A builder chat edits a draft through safe tools (the model
  never writes TOML), a live preview shows actions, tables, settings, exports,
  tools and permissions, strict checks block broken drafts, and installing
  goes through the normal review (with a what-changed diff for edits).
- Approval-gated test runs of an agent's generated Python tools in a subprocess.
- New agent dialog: *Make with AI*, *Quick* (prompt only) or *Copy an agent*.
- Drafts in progress are listed on the Agents page; unfinished parts show as
  *Agent todo* items, separate from real problems.
- **Thinking control** under the message box: Auto / Off / Low / Medium / High
  reasoning effort and the *Max output* budget, translated for each provider
  (OpenAI, Gemini, DeepSeek, OpenRouter, ChatGPT, Anthropic, local servers).

**Fixed**
- Replies cut off by the output limit no longer end the turn silently: Jarvis
  reads the model's stop reason, retries once with double the limit, and
  otherwise says so and offers Continue. A cut-off tool call is never run.
- Default max output raised from 4,096 to 16,384 tokens (thinking models used
  the whole budget on reasoning); servers with a lower cap are detected.
- An agent chat opened mid-turn could stay stuck on "working".
- Pending approvals now come back when a chat is reloaded.

### 1.2.0 — 2026-11-26

**New**
- **Cloudflare Workers AI** and **Cloudflare AI Gateway** providers.

### 1.1.2 — 2026-11-25

**New**
- **Agents**: specialised agent plugins with a standard folder format
  (`agent.toml`), settings, actions with inputs, records tables, exports,
  optional tools and skills, and their own chat. Import from zip/GitHub/folder
  with a review step, export, duplicate, create. Built-in **Lead Finder**.
- **Routines** replace cron jobs: in-process scheduler, plain-English or cron
  schedules with a live preview, agent actions as targets, run history,
  notifications, catch-up after downtime, `jarvis --daemon`.
- **Discussions**: agenda-driven group chats between 2–6 agents on any
  providers, four modes, user participation, item summaries and minutes.
- **Claude-style sidebar** with pages for Agents, Discussions, Routines and Tools.
- **Saved API keys** ("Remember on this computer", Forget).
- **Harness**: parallel read-only tool calls, `read_file` ranges, `edit_file`,
  `grep`, `glob`, large outputs spilled to files, background commands, MCP
  client, `spawn_agent` subagents, `web_search` (Serper/Brave/free) and
  `fetch_page` with contact extraction, 50 tool rounds + Continue.

**Improved**
- On PyPI as `jarvis-harness` (`pipx install jarvis-harness`). Installed copies keep
  their data in `~/.jarvis`; a git clone still uses its own folder.
- DeepSeek thinking mode keeps its reasoning across tool calls.
- Several agents can run at once; approvals reach the right one.

### 1.1.1 — 2026-11-25

- ChatGPT sign-in reports the real result; the UI only shows signed-in when
  the plan is usable.
- Settings: grouped provider list, searchable models, key status, Gemini key
  field fixed, sensible default models.
- Gemini tool calls keep their thought signatures.
- Chat: live thinking, `<think>` parsing, tokens/sec, safe markdown, readable
  errors, Stop keeps partial text, retry no longer duplicates messages.

### 1.1 — 2026-11-24

**New**
- **Web UI** (`jarvis --web`): streaming chat, reasoning view, tool-call cards,
  inline approvals, artifacts, sessions, and panels for Characters, Mind,
  Memory, Tools & Skills, Jobs, Voice and Settings. Local-only with a per-run
  token.
- **Welcome tour** on first launch: connect a model, pick a character, quick tips.
- **Sign in with ChatGPT**: use your ChatGPT Plus/Pro plan instead of an API key.
- **Anthropic (Claude)** via the official SDK, with prompt caching and adaptive
  thinking.
- More providers: OpenRouter, Groq, DeepSeek, Mistral, xAI, vLLM.
- **Switch provider/model mid-chat** from the header; quota errors offer a switch.
- **Character cards**: import SillyTavern cards (JSON/PNG), edit as simple
  fields, export, hot-swap mid-chat. Built-ins: Jarvis, Jarvis (Casual), J-7
  (Robotic).
- **Message actions**: Edit & resend your messages; Copy, Re-do response and
  Speak on replies.
- **Voice**: Speak button and Auto-speak toggle (off by default), browser
  playback; Piper default, Kokoro one-click optional; all models downloadable
  from the Voice tab with sizes shown.
- **Reasoning display** for models that expose it (local reasoning models,
  Claude thinking summaries).
- `/voice`, `/character export|reset`, `/login`, `/logout` commands.

**Improved**
- **TurboMCP tool search**: the model sees ~9 tool schemas instead of every
  tool (about 60% fewer tool tokens today, and new tools cost nothing up front).
- **Prompt stability**: the system prompt is built once per turn (date only, no
  clock), so prompt caching works and local servers don't re-read everything
  each tool round. The memory index in the prompt is capped.
- **One persona source**: personality lives in character cards; your
  `personality.md` was migrated once. Learned preferences go to `user.md`.
- Semantic tool search uses the embedding model only if you downloaded it —
  nothing downloads in the background.
- Everything installs with a single `pip install` (no optional extras).
- Smarter LLM retries: only network errors, rate limits and server errors are
  retried.

**Fixed**
- Codified skill tools now go through approvals (risky commands were silently
  denied before) and no longer dump every tool to the model.
- The tool-budget wrap-up keeps the tool list, so APIs that require it don't
  reject the request.
- Opening Jarvis no longer leaves empty "New conversation" sessions behind.

### 1.0

The original release: terminal UI with waveform orb, agent loop with streaming
and tool calling, history summarization, memory (facts, episodes, dream-cycle
consolidation, review and privacy rules), skills → tools self-improvement,
artifacts, Piper/Kokoro voice with push-to-talk, cron jobs, supervisor, MCP
server, slash commands with a provider manager, and OpenAI-compatible providers
(LM Studio, Ollama, llama.cpp, OpenAI, Gemini).

## Roadmap

- **Code mode**: the model writes a short script that calls tools; only its
  output returns to context (big savings for batch work).
- **Sandbox**: optional bubblewrap isolation for shell commands.
- **Skills & MCP marketplace**: browse ready-made skills and tools (for
  example "give it data, get a chart") from a GitHub catalog, install with one
  click, or add your own GitHub links.

## License

MIT
