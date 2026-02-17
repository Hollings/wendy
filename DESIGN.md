# Wendy v2 -- Design Document

A clean reimplementation of the Wendy Discord bot. Same user-facing features, dramatically simpler internals.

---

## What Is Wendy?

Wendy is a Discord bot powered by **Claude Code CLI** -- the subscription-based CLI tool, not the Anthropic API. When someone messages her in a whitelisted Discord channel, the bot spawns a `claude` subprocess, which reads messages, thinks, runs tools (shell, files, web), and sends replies back through an HTTP API.

The unusual part: Wendy doesn't call the Anthropic API. She runs the `claude` command-line tool as a child process, inheriting all of Claude Code's capabilities (shell, file I/O, web search, code execution) as native Discord abilities.

**GitHub:** `github.com/Hollings/wendy`

---

## Why Rewrite?

The current system (wendy-bot v1) grew organically into 3 separate Docker services that share a Dockerfile, communicate through files on disk, and duplicate schema/config in multiple places. The architecture works but has accumulated significant complexity:

| Problem | Details |
|---|---|
| 3 services that should be 1 | bot, proxy, orchestrator share a Dockerfile, two volumes, and import from each other's modules |
| File-based message passing | Proxy writes JSON to an outbox directory, bot polls it every 0.5s -- adds latency and complexity |
| Schema in 4 places | SQLite table definitions duplicated across state_manager, message_logger, proxy, and wendy-sites |
| God modules | claude_cli.py (1300 lines), proxy/main.py (1400 lines), state_manager.py (1100 lines) |
| Migration cruft | Multiple one-time migration systems with marker files that will never run again |
| Layered behavior injection | System prompt + 9 fragment layers + legacy CLAUDE.md + journal nudges + hooks = hard to debug |

---

## Architecture: v1 vs v2

### v1 (current)

```
+-----------+     +-------+     +--------------+
|    bot    |     | proxy |     | orchestrator |
|  discord  |     | :8945 |     |  background  |
|  gateway  |     |  API  |     |    tasks     |
+-----+-----+     +---+---+     +------+-------+
      |               |                |
      +-------+-------+--------+-------+
              |     shared volumes     |
              v                        v
       wendy_data              claude_config
       /data/wendy/            /root/.claude/
```

- 3 Docker containers, 1 shared Dockerfile
- Bot -> Claude CLI -> curl proxy -> JSON file to outbox -> bot polls outbox -> discord.py -> Discord
- Orchestrator is a separate polling loop in a separate container

### v2 (target)

```
+------------------------------------------+
|            wendy (single process)        |
|                                          |
|  +----------------+  +---------------+   |
|  | Discord client |  | HTTP server   |   |
|  | (discord.py)   |  | (localhost)   |   |
|  +-------+--------+  +-------+-------+   |
|          |                    ^           |
|          v                    |           |
|  +----------------+    curl localhost     |
|  | CLI manager    +----------+           |
|  | (subprocess)   |                      |
|  +----------------+                      |
|                                          |
|  +----------------+  +---------------+   |
|  | Task runner    |  | State (SQLite)|   |
|  | (asyncio)      |  | (single file) |   |
|  +----------------+  +---------------+   |
+------------------------------------------+
```

- 1 Docker container, 1 process
- Bot -> Claude CLI -> curl localhost (in-process HTTP) -> discord.py -> Discord (no outbox, no polling)
- Task runner is an asyncio background task in the same process

---

## Core Data Flow (v2)

```
(1) Discord message arrives
         |
         v
(2) discord_client.py: on_message
    - Filter by channel whitelist
    - Cache message to SQLite
    - Save attachments to channel dir
    - Decide whether to respond
         |
         v
(3) cli.py: spawn_cli()
    - Build system prompt from fragments
    - Spawn `claude` subprocess
    - Send nudge prompt via stdin
    - Read streaming JSON output
    - Track token usage
         |
         | Claude CLI runs autonomously...
         | uses curl to talk back:
         |
         v
(4) api_server.py: POST /api/send_message
    - Validate message
    - Check for new-message interrupts
    - Call discord_client.send() directly  <-- NO outbox, NO file, NO polling
         |
         v
(5) Discord channel gets the message
```

**What changed:** Steps 4-5 collapsed from "proxy writes JSON file -> outbox cog polls every 0.5s -> sends via discord.py" to "API server calls discord.py directly." Same security model (Claude CLI never sees the Discord token), zero latency from file polling.

---

## Security Model

Claude CLI runs as a subprocess without access to sensitive credentials. This is unchanged from v1:

1. `SENSITIVE_ENV_VARS` are filtered from the subprocess environment before spawning
2. Claude CLI only knows `curl http://localhost:{port}/api/...`
3. The HTTP server validates all requests and controls what actions are allowed
4. The Discord token lives only in the parent process memory

The trust boundary is the HTTP API, not container isolation. This was true in v1 too -- the proxy ran on the same host network. The security model is identical.

---

## Module Structure

```
wendy/
  __main__.py           Entry point. asyncio.run(), starts all subsystems.
  discord_client.py     Discord gateway. on_message, send_to_channel, attachment saving.
  api_server.py         Internal HTTP server (aiohttp). Endpoints Claude CLI curls.
  cli.py                Spawn and stream Claude CLI subprocess. Nothing else.
  prompt.py             Assemble system prompt from fragments + tool instructions.
  sessions.py           Session lifecycle: create, resume, truncate, recover.
  fragments.py          Fragment loader (port from v1 fragment_loader.py, mostly as-is).
  fragment_setup.py     Seed fragments from config/ to /data/wendy/ at startup.
  tasks.py              Beads background task runner (asyncio, replaces orchestrator).
  state.py              SQLite state manager. ONE schema definition. Period.
  paths.py              All filesystem paths. Leaf module, zero internal imports.
  models.py             Dataclasses: ChannelConfig, SessionInfo, Notification, etc.
  message_logger.py     Message caching to SQLite (port from v1).
  config.py             Parse WENDY_CHANNEL_CONFIG, model map, constants.
```

### Import Hierarchy

```
paths.py, models.py, config.py    (leaf modules, no internal imports)
         |
         v
state.py                          (imports: paths, models)
         |
         v
fragments.py                      (imports: paths, state)
fragment_setup.py                  (imports: paths)
sessions.py                       (imports: paths, state, config)
message_logger.py                  (imports: paths, state)
         |
         v
prompt.py                         (imports: paths, fragments, config)
cli.py                            (imports: paths, sessions, prompt, state, config)
tasks.py                          (imports: paths, sessions, cli, state, config)
         |
         v
api_server.py                     (imports: state, message_logger, paths, config)
discord_client.py                 (imports: cli, api_server, tasks, state,
                                            message_logger, fragment_setup, config)
         |
         v
__main__.py                       (imports: discord_client)
```

No circular imports. Clear dependency direction. Every module has a single responsibility.

---

## Feature Parity: v1 -> v2 Mapping

### Core Features

| Feature | v1 Implementation | v2 Implementation |
|---|---|---|
| Discord message handling | `wendy_cog.py` (850 lines) | `discord_client.py` (~400 lines) |
| Claude CLI spawning | `claude_cli.py` (1300 lines, mixed concerns) | `cli.py` (~300 lines, subprocess only) |
| System prompt assembly | `claude_cli.py:_build_system_prompt()` | `prompt.py` (~200 lines, dedicated module) |
| Session management | `claude_cli.py` (mixed into generator) | `sessions.py` (~200 lines, dedicated module) |
| Fragment loading | `fragment_loader.py` (475 lines) | `fragments.py` (port as-is, already clean) |
| Fragment seeding | `fragment_setup.py` (180 lines) | `fragment_setup.py` (simplified, no migration code) |
| HTTP API for Claude | `proxy/main.py` (1400 lines, separate service) | `api_server.py` (~500 lines, in-process) |
| Message queue to Discord | `wendy_outbox.py` (450 lines, file polling) | Eliminated. API server calls discord.py directly |
| Background tasks (Beads) | `orchestrator/main.py` (1170 lines, separate service) | `tasks.py` (~200 lines, asyncio in-process) |
| SQLite state | `state_manager.py` (1100 lines, schema in 4 places) | `state.py` (~400 lines, schema in 1 place) |
| Message caching | `message_logger.py` (400 lines) | `message_logger.py` (~300 lines, simplified) |
| Paths | `paths.py` | `paths.py` (same) |

### What Gets Deleted

| v1 Component | Why It's Gone |
|---|---|
| `proxy/` directory (entire service) | Merged into `api_server.py` in the main process |
| `orchestrator/` directory (entire service) | Merged into `tasks.py` in the main process |
| `wendy_outbox.py` | No outbox needed when API server has direct discord.py access |
| `conversation.py` | Dataclasses moved to `models.py` |
| All migration code | Ship clean. If live server needs migration, write a one-time script |
| Legacy CLAUDE.md fallback | Fragments fully replace this. Clean break |
| `claude_fragments.json` | Already deleted in v1; frontmatter is the system |
| Schema duplication | One definition in `state.py`, nothing else creates tables |
| `docker-compose.yml` services for proxy/orchestrator | One service, one container |

### What Stays Exactly The Same

| Component | Notes |
|---|---|
| Fragment file format | YAML frontmatter + markdown body. Same schema, same files |
| Fragment selection logic | Keywords, match_authors, select snippets. Port verbatim |
| Claude CLI invocation flags | `-p`, `--output-format stream-json`, `--resume`, etc. |
| System prompt content | `system_prompt.txt` unchanged |
| Tool instructions template | Same curl commands, same endpoints, same format |
| Hooks system | Claude Code hooks are external; just ship the same `claude_settings.json` |
| Session file format | Claude CLI manages these; we just store the UUID |
| SQLite tables | Same tables, just defined in one place |
| Channel config format | Same `WENDY_CHANNEL_CONFIG` JSON array |
| Filesystem layout | Same `/data/wendy/` structure |
| Deployment model | Same Docker on Orange Pi, same volumes |

---

## System Prompt Assembly (v2)

`prompt.py` builds the full system prompt. Same 9-layer order as v1:

```
[1] Base system prompt            <- config/system_prompt.txt (static)
[2] Persons section               <- person_*.md fragments (keyword/author matched)
[3] Channel section               <- common_*.md + {channel_id}_*.md fragments
[4] Tool instructions             <- TOOL_INSTRUCTIONS_TEMPLATE (with channel_id, port)
[5] Journal section               <- journal nudge + file listing
[6] Beads warning                 <- active task count if any
[7] Thread context                <- parent channel info if in a thread
[8] Topics section                <- topic_*.md fragments (keyword/select matched)
[9] Anchors section               <- anchor_*.md fragments (always loaded)
```

The key improvement: this is a dedicated module (~200 lines) instead of being buried in the 1300-line claude_cli.py.

---

## API Server Endpoints (v2)

Same endpoints Claude CLI expects, running in-process on localhost:

| Endpoint | Method | Purpose | v1 Location |
|---|---|---|---|
| `/api/send_message` | POST | Send message to Discord (with interrupt detection) | proxy/main.py |
| `/api/check_messages/{channel_id}` | GET | Fetch recent messages + task updates | proxy/main.py |
| `/api/emojis` | GET | List custom server emojis | proxy/main.py |
| `/api/usage` | GET | Claude Code usage stats | proxy/main.py |
| `/api/usage/refresh` | POST | Force usage check | proxy/main.py |
| `/api/deploy_site` | POST | Deploy to wendy.monster | proxy/main.py |
| `/api/deploy_game` | POST | Deploy game server | proxy/main.py |
| `/api/analyze_file` | POST | Analyze media via Gemini | proxy/main.py |

The new-message interrupt system works identically:
1. `check_messages` records last seen message ID in SQLite
2. `send_message` checks if new messages arrived since last check
3. If yes, returns them instead of sending (Claude re-reads and retries)

---

## Background Tasks / Beads (v2)

Replaces the orchestrator service with an asyncio task in the main process:

```python
# tasks.py (simplified)
class TaskRunner:
    async def run(self):
        """Main polling loop - runs as asyncio.create_task()"""
        while True:
            for channel in self.beads_channels:
                tasks = self.scan_beads_dir(channel)
                for task in tasks:
                    if self.can_start(task) and self.under_concurrency_limit():
                        asyncio.create_task(self.run_agent(task))
            await asyncio.sleep(30)

    async def run_agent(self, task):
        """Fork session and run agent subprocess"""
        # Same logic as orchestrator/main.py but ~200 lines instead of 1170
```

Same behavior: polls `.beads/` directories, forks sessions, spawns Claude CLI agents, writes notifications on completion. Just no separate container.

---

## SQLite State (v2)

One file: `state.py`. One schema definition. One `StateManager` class.

### Tables

| Table | Purpose | Writer | Reader |
|---|---|---|---|
| `channel_sessions` | Session IDs, token usage, folder mapping | bot | bot |
| `channel_last_seen` | New-message interrupt tracking | api_server | api_server |
| `message_history` | Full message archive | message_logger | api_server, fragments |
| `notifications` | Task completions, webhook events | tasks, wendy-sites | bot |
| `thread_registry` | Thread-to-parent channel mappings | bot | api_server |
| `usage_state` | Usage notification thresholds | tasks | tasks |
| `bash_tool_log` | Every bash command Claude runs | hook script | queryable |

**Deleted tables:** `task_completions` (legacy), `webhook_events` (legacy). Notifications table handles both.

---

## Session Management (v2)

`sessions.py` -- dedicated module, ~200 lines.

```
Per-channel session lifecycle:
  1. First message    -> create_session() -> new UUID, store in SQLite
  2. Next messages    -> get_session()    -> resume with --resume {id}
  3. 50+ messages     -> truncate()       -> trim old messages from .jsonl
  4. Corrupt session  -> recover()        -> retry with fresh session
```

Session files live at `/root/.claude/projects/-data-wendy-channels-{name}/{uuid}.jsonl` (Claude CLI manages these; we just track the UUID).

Thread support: threads get their own workspace at `/data/wendy/channels/{parent}_t_{thread_id}/` with sessions forked from the parent via `--resume {parent_id} --fork-session`.

---

## Fragment System (v2)

Port `fragment_loader.py` essentially as-is -- it's already clean after the v1 refactor.

### Fragment Types

| Type | When Loaded | Example |
|---|---|---|
| `common` | Always | `common_01_communication_style.md` |
| `channel` | Channel ID matches | `1461429474250850365_01_test_pi.md` |
| `person` | Keywords/authors match (or always if no rules) | `person_01_hollings.md` |
| `topic` | Keywords/select match recent messages | `topic_01_runescape.md` |
| `anchor` | Always | `anchor_01_behavior.md` |

### Frontmatter Schema

```yaml
---
type: topic
order: 1
keywords: [osrs, runescape, bond]
match_authors: true
select: |
  return any("webhook" in a for a in authors)
---
# Content here...
```

### What Changes

- **No migration code.** `fragment_setup.py` just seeds files from `config/` to `/data/wendy/`. No `.migrated` markers, no `.frontmatter_migrated` markers, no legacy prompts migration. If the live server needs a one-time migration, write a standalone script.
- **No legacy CLAUDE.md fallback.** Fragments are the system. Period.

---

## Hooks (v2)

Claude Code hooks are external to our code -- they're defined in `claude_settings.json` and run by the Claude CLI process. We just ship the config file.

### Active Hooks

| Hook | Matcher | Script | Purpose |
|---|---|---|---|
| PreToolUse | `Task` | (inline) | Blocks Task tool, forces `bd` (Beads) |
| PostToolUse | `Read` | `hooks/remind_analyze_file.sh` | Reminds to call analyze_file for images |
| PostToolUse | `Bash` | `hooks/log_bash_tool.sh` (async) | Logs bash commands to SQLite |
| Stop | (all) | `hooks/journal_stop_check.sh` | Nudges journal writing before exit |
| Stop | (all) | `hooks/prompt_bookkeeping.sh` | Nudges fragment file updates before exit |

These are copied verbatim from v1.

---

## Docker Setup (v2)

### docker-compose.yml

```yaml
version: '3.8'

services:
  wendy:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    container_name: wendy
    restart: unless-stopped
    command: ["python", "-m", "wendy"]
    network_mode: host
    volumes:
      - wendy_data:/data/wendy
      - claude_config:/root/.claude
      - /srv/secrets/wendy:/secrets:ro
      - /srv/wendy-bot/config:/app/config:ro
      - /var/run/docker.sock:/var/run/docker.sock
    env_file:
      - /srv/secrets/wendy/bot.env
    environment:
      - WENDY_DB_PATH=/data/wendy/shared/wendy.db
      - SYSTEM_PROMPT_FILE=/app/config/system_prompt.txt
      - CLAUDE_CLI_TIMEOUT=300
      - WENDY_PROXY_PORT=8945
      - ORCHESTRATOR_CONCURRENCY=3
      - GEMINI_API_KEY=${GEMINI_API_KEY:-}

volumes:
  wendy_data:
    external: true
    name: wendy_data
  claude_config:
    external: true
    name: claude_config
```

One service. Same volumes. Same secrets. wendy-sites, wendy-games, and wendy-avatar remain separate (they're legitimately different tech stacks).

---

## Channel Configuration

Unchanged from v1:

```json
[
  {"id": "123", "name": "chat", "mode": "chat"},
  {"id": "456", "name": "coding", "mode": "full", "model": "opus", "beads_enabled": true}
]
```

Parsed once at startup in `config.py`. No other module re-parses it.

---

## Filesystem Layout

Same as v1. Nothing changes on disk:

```
/data/wendy/
+-- channels/                    Per-channel workspaces
|   +-- {name}/                  Each channel gets isolated workspace
|       +-- attachments/         Downloaded Discord files
|       +-- journal/             Long-term memory entries
|       +-- .claude/             Claude Code settings (hooks)
|       +-- .beads/              Task queue (if beads_enabled)
|       +-- .current_session     Session ID for agent forking
+-- claude_fragments/            Fragment files (YAML frontmatter)
+-- shared/
|   +-- wendy.db                 SQLite database (all state)
+-- secrets/
|   +-- runtime.json             Runtime secrets (writable by Wendy)
+-- stream.jsonl                 Rolling event log
+-- tmp/                         Scratch space
```

**Deleted:** `shared/outbox/` directory (no longer needed).

---

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `DISCORD_TOKEN` | Discord bot token | (required) |
| `WENDY_CHANNEL_CONFIG` | JSON array of channel configs | (required) |
| `WENDY_DB_PATH` | SQLite database path | `/data/wendy/shared/wendy.db` |
| `SYSTEM_PROMPT_FILE` | Path to system prompt | `/app/config/system_prompt.txt` |
| `CLAUDE_CLI_TIMEOUT` | Max seconds for CLI response | `300` |
| `WENDY_PROXY_PORT` | Port for internal HTTP server | `8945` |
| `JOURNAL_NUDGE_INTERVAL` | Invocations between journal nudges | `10` |
| `MESSAGE_LOGGER_GUILDS` | Comma-separated guild IDs for archival | (optional) |
| `GEMINI_API_KEY` | Google Gemini API key for file analysis | (optional) |
| `ORCHESTRATOR_CONCURRENCY` | Max concurrent background agents | `3` |
| `ORCHESTRATOR_POLL_INTERVAL` | Seconds between task checks | `30` |
| `ORCHESTRATOR_AGENT_TIMEOUT` | Max agent runtime in seconds | `1800` |

---

## Model Configuration

```python
# config.py
MODEL_MAP = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-5-20250929",
    "haiku": "claude-haiku-4-5-20251001",
}
```

Defined in ONE place. Used by both CLI spawning and task runner.

---

## Implementation Phases

### Phase 1: Core Loop

Get a message in Discord, spawn Claude CLI, get a response back to Discord.

Files: `__main__.py`, `discord_client.py`, `api_server.py`, `cli.py`, `paths.py`, `config.py`, `models.py`, `state.py`

**Acceptance:** Send "what is 2+2?" in Discord, get a response back.

### Phase 2: Prompt Assembly

Dynamic system prompts from fragments.

Files: `prompt.py`, `fragments.py`, `fragment_setup.py`

**Acceptance:** Wendy loads the right fragments per channel/context.

### Phase 3: Session Management

Persistent sessions with truncation and recovery.

Files: `sessions.py`

**Acceptance:** Wendy remembers context across messages, truncates at 50 messages, recovers from corrupt sessions.

### Phase 4: Production Hardening

Message caching, new-message interrupts, attachment handling, thread support.

Files: `message_logger.py`, updates to `api_server.py` and `discord_client.py`

**Acceptance:** Full interrupt system works, threads get isolated workspaces, attachments download correctly.

### Phase 5: Background Tasks

Beads integration.

Files: `tasks.py`

**Acceptance:** `bd create` queues a task, runner picks it up, forks session, spawns agent, notifies on completion.

### Phase 6: Everything Else

Journal system, webhook handling, deploy_site/deploy_game proxying, emoji search, usage tracking, Gemini file analysis.

**Acceptance:** Full feature parity with v1.

---

## Key Design Decisions

### Why aiohttp for the internal server (not FastAPI)?

FastAPI requires uvicorn as a separate ASGI server. aiohttp can run embedded in our asyncio event loop with zero additional processes. Since this is an internal-only localhost API that Claude CLI curls, we don't need FastAPI's fancy features (OpenAPI docs, pydantic validation, etc.). Raw aiohttp routes are simpler.

Alternative: FastAPI with `uvicorn.Server` running in-process is also viable but adds dependencies.

### Why not just use the Anthropic API?

Claude Code CLI provides the full Claude Code subscription quota instead of per-token API billing. For a personal bot that runs continuously, this is dramatically cheaper. The CLI also provides built-in tool use, session management, and the hooks system that would need to be reimplemented with raw API calls.

### Why keep the HTTP API instead of using pipes/signals?

Claude CLI is designed to call external tools via `curl`. The system prompt tells it the endpoint URLs. Changing this to pipes or IPC would require modifying Claude CLI's behavior, which we don't control. HTTP localhost is the natural interface.

### Why single process instead of microservices?

The bot, proxy, and orchestrator are all Python, share a Dockerfile, share two Docker volumes, and import from each other's modules. They access the same SQLite database. They run on the same machine. There is zero benefit to container isolation between them -- the security boundary is the HTTP API between Claude CLI (untrusted subprocess) and the bot (trusted parent), not between bot/proxy/orchestrator (all trusted).

---

## Migration Strategy

### For the live server

1. Deploy wendy-v2 alongside wendy-v1 (different container name)
2. Point one test channel at v2
3. Validate feature parity
4. Switch remaining channels
5. Remove v1 containers

### For the data volume

The `/data/wendy/` volume is unchanged. Fragment files, channel workspaces, the SQLite database, journal entries -- all compatible. The only cleanup is deleting `/data/wendy/shared/outbox/` (no longer used).

### For session files

The `/root/.claude/` volume is unchanged. Session files are managed by Claude CLI and are format-compatible.

---

## Constants and Limits (ported from v1)

| Constant | Value | Purpose |
|---|---|---|
| `MAX_DISCORD_MESSAGES` | 50 | Session truncation threshold |
| `MAX_STREAM_LOG_LINES` | 5000 | Rolling stream.jsonl limit |
| `JOURNAL_NUDGE_INTERVAL` | 10 | Invocations between journal nudges |
| `SENSITIVE_ENV_VARS` | 10 vars | Filtered from CLI subprocess environment |
| `PROXY_PORT` | 8945 | Internal HTTP server port |
| `WENDY_USER_ID` | 771821437199581204 | Wendy's Discord user ID (for message filtering) |

---

## Dependencies

```
# requirements.txt
discord.py>=2.3.0
pyyaml>=6.0
aiohttp>=3.9.0
```

That's it. No FastAPI, no uvicorn, no httpx, no pydantic, no python-multipart. The proxy's heavyweight dependencies are gone because we're using aiohttp for a simple localhost API.

---

## Reference: v1 Source Files

For porting logic, these are the key v1 files and what to extract from each:

| v1 File | Lines | Port To | What to Extract |
|---|---|---|---|
| `bot/claude_cli.py` | ~1300 | `cli.py`, `prompt.py`, `sessions.py` | CLI spawning, prompt building, session management |
| `bot/wendy_cog.py` | ~850 | `discord_client.py` | on_message, channel filtering, attachment saving |
| `bot/wendy_outbox.py` | ~450 | (deleted) | Nothing -- replaced by direct discord.py calls |
| `bot/state_manager.py` | ~1100 | `state.py` | SQLite schema and methods (simplified) |
| `bot/message_logger.py` | ~400 | `message_logger.py` | Message caching (remove schema duplication) |
| `bot/fragment_loader.py` | ~475 | `fragments.py` | Port nearly verbatim |
| `bot/fragment_setup.py` | ~180 | `fragment_setup.py` | Seeding only, delete migration code |
| `bot/paths.py` | ~100 | `paths.py` | Port as-is |
| `bot/conversation.py` | ~50 | `models.py` | Dataclasses |
| `proxy/main.py` | ~1400 | `api_server.py` | Endpoint logic (strip FastAPI boilerplate) |
| `orchestrator/main.py` | ~1170 | `tasks.py` | Polling loop, agent spawning, notification |

**Total v1:** ~7800 lines across 11 files + 2 services
**Estimated v2:** ~3000 lines across 14 files, 1 service
