# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Wendy is a Discord bot powered by Claude Code CLI. She runs as multiple Docker services on an Orange Pi home server, using the Claude Code subscription (not API credits) for LLM inference.

**GitHub:** github.com/Hollings/wendy (separate repo from cee-wtf monorepo)

## Development Commands

```bash
# Run tests (use python3 on Linux/WSL where 'python' may not be linked)
python3 -m pytest tests/
python3 -m pytest tests/test_claude_cli.py -v              # Single file
python3 -m pytest tests/test_claude_cli.py::TestCountDiscordMessages -v  # Single class
python3 -m pytest tests/ --cov=bot --cov=proxy --cov=orchestrator  # With coverage
python3 -m pytest tests/ -s  # Show print output

# Lint
ruff check .
ruff check --fix .
```

CI runs lint (ruff) and tests (pytest with Python 3.11) on push/PR to main via `.github/workflows/test.yml`.

## Architecture

```
Discord <-> Bot (Claude Code CLI) <-> Proxy <-> Discord API
                |
                v
         Orchestrator --> Background Agents (tasks)
                |
                +---> wendy-sites (wendy.monster)
                +---> wendy-games (game servers)
                +---> wendy-avatar (3D visualization)
```

### Services

| Service | Port | Entry Point |
|---------|------|-------------|
| bot | (network_mode: host) | `python -m bot` |
| proxy | 8945 | `uvicorn proxy.main:app` |
| orchestrator | (network_mode: host) | `python -m orchestrator` |
| wendy-sites | 8910 | Hosts wendy.monster + Brain dashboard |
| wendy-games | 8920 | WebSocket game server manager |
| wendy-avatar | 8915 | 3D visualization (Three.js) |

Bot, proxy, and orchestrator share a single Dockerfile and two external Docker volumes: `wendy_data` (mounted at `/data/wendy`) and `claude_config` (mounted at `/root/.claude`).

### Key Data Flow

1. **Discord -> Bot**: `WendyCog.on_message` receives messages, caches to SQLite via `MessageLoggerCog`, saves attachments per-channel
2. **Bot -> Claude CLI**: `ClaudeCliTextGenerator.generate()` spawns `claude` subprocess with `--resume` for session persistence, sends nudge prompt via stdin
3. **Claude CLI -> Proxy**: Claude calls `curl` to hit proxy API endpoints (`/api/send_message`, `/api/check_messages`)
4. **Proxy -> Outbox**: Proxy writes JSON files to `/data/wendy/shared/outbox/`
5. **Outbox -> Discord**: `WendyOutbox` cog polls outbox dir every 0.5s and sends via discord.py

### Module Structure

| Module | Key File(s) | Responsibility |
|--------|-------------|----------------|
| `bot/claude_cli.py` | ~1200 lines | Spawns Claude CLI, manages sessions, handles streaming output |
| `bot/wendy_cog.py` | ~750 lines | Discord event handling, message routing, attachment saving |
| `bot/wendy_outbox.py` | ~450 lines | Watches outbox dir, sends messages/reactions to Discord |
| `bot/state_manager.py` | ~1000 lines | Unified SQLite state (sessions, last_seen, notifications, usage) |
| `bot/message_logger.py` | ~400 lines | Message caching and archival |
| `bot/paths.py` | Centralized paths | All filesystem paths - use this instead of constructing paths manually |
| `bot/conversation.py` | Data structures | Conversation/message dataclasses |
| `proxy/main.py` | ~1400 lines | FastAPI proxy: all API endpoints |
| `orchestrator/main.py` | ~1200 lines | Background task polling, agent spawning, usage monitoring |

### Proxy API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/send_message` | POST | Queue message to Discord (with new-message interrupt detection) |
| `/api/check_messages/{channel_id}` | GET | Fetch messages and task updates |
| `/api/emojis` | GET | List custom server emojis (searchable) |
| `/api/usage` | GET | Claude Code usage stats |
| `/api/usage/refresh` | POST | Force immediate usage check |
| `/api/deploy_site` | POST | Deploy static site to wendy.monster |
| `/api/deploy_game` | POST | Deploy multiplayer game backend |
| `/api/game_logs/{name}` | GET | Fetch game server logs |
| `/api/analyze_file` | POST | Analyze media via Gemini API (images, audio, video) |

### Outbox Action Types

The outbox supports batch actions via an `actions` array in the JSON file:

- **`send_message`**: `{type, content, file_path?, reply_to?}` - send text/attachment
- **`add_reaction`**: `{type, message_id, emoji}` - add emoji reaction to a message

### New Message Interrupts

Prevents stale replies when users send messages while Wendy is thinking:
1. `check_messages` records the last seen message ID in SQLite
2. `send_message` checks if new real messages (not synthetic, ID < 9e18) arrived since last check
3. If new messages exist, returns them instead of sending (409-like response with guidance)
4. Claude must re-read messages and retry with updated response

### Filesystem Layout (on server)

```
/data/wendy/
+-- channels/              # Per-channel workspaces (cwd for Claude CLI)
|   +-- {name}/            # Each channel gets isolated workspace
|       +-- CLAUDE.md      # Wendy's self-editable notes (loaded as system prompt)
|       +-- attachments/   # Downloaded Discord files (per-channel isolation)
|       +-- journal/       # Long-term memory entries (auto-nudged)
|       +-- .claude/       # Claude Code settings (hooks config)
|       +-- .beads/        # Task queue (only if beads_enabled)
|       +-- .current_session  # Session ID for agent forking
+-- shared/
|   +-- outbox/            # Message queue to Discord
|   +-- wendy.db           # SQLite database (all state)
+-- stream.jsonl           # Rolling log of Claude CLI events (max 5000 lines)
+-- tmp/                   # Scratch space
```

### Session Management

Claude CLI sessions are per-channel with automatic truncation:
- Sessions stored in `/root/.claude/projects/-data-wendy-channels-{name}/` (path encoding replaces `/` with `-`)
- Session state (ID, token counts) tracked in SQLite `channel_sessions` table
- Truncates when Discord messages in session exceed `MAX_DISCORD_MESSAGES` (50) - see `claude_cli.py:_truncate_session_if_needed`

### Claude CLI Invocation

Key flags passed to the `claude` subprocess:
- `-p` (headless mode, no interactive prompts)
- `--output-format stream-json` (streaming JSON output)
- `--verbose` (debug logging)
- `--resume {session_id}` / `--session-id {id}` (session persistence)
- `--append-system-prompt` (dynamic system prompt injection)
- `--allowedTools` / `--disallowedTools` (channel-mode-based permissions)

Tool permissions vary by channel mode:
- **"chat" mode**: Read, WebSearch, WebFetch, limited Bash, Edit/Write restricted to own channel folder
- **"full" mode**: All tools except Edit/Write to `/app/**` files

### Journal System

Per-channel journal at `/data/wendy/channels/{name}/journal/`:
- Auto-nudges Wendy to write entries every `JOURNAL_NUDGE_INTERVAL` invocations (default 10)
- Tracked via invocation counter in state manager
- Stop hook (`config/hooks/journal_stop_check.sh`) checks journal before CLI exits

### Background Task System (Beads)

Beads is enabled per-channel via `beads_enabled: true` in channel config:
1. Wendy runs `bd create "task description"` to queue a task
2. Orchestrator forks Wendy's current session (`--resume --fork-session`)
3. Agent works autonomously, uses `bd close` when done
4. Orchestrator writes to `notifications` table in SQLite
5. Bot's `watch_notifications` loop (5s interval) wakes Wendy

### Notifications System

Unified via `notifications` table in SQLite:
- **Writers**: orchestrator (task completions), wendy-sites (webhooks)
- **Readers**: bot (`watch_notifications` loop), proxy (task updates for Claude)
- Separate `seen_by_wendy` and `seen_by_proxy` flags for independent processing
- Synthetic messages (ID >= 9e18) are one-time: shown to Claude once then deleted from `message_history`

### File Analysis (Gemini)

The `/api/analyze_file` endpoint uses Gemini for multimodal analysis:
- Supports images, audio (max 30min), and video (max 5min, 20MB)
- Model selection: `gemini-2.5-pro` for video, `gemini-3-pro-preview` for other media
- Video resolution auto-scaled based on duration to manage tokens
- Requires `GEMINI_API_KEY` env var

## Important Gotchas

### Duplicate Schema Definition

The SQLite schema is defined in **3 places** that must stay in sync:
1. `bot/state_manager.py:_init_schema()` - **primary source of truth**
2. `bot/message_logger.py:_init_db()` - copy for startup ordering
3. `wendy-sites/backend/main.py` - notifications only, separate container

### Model IDs

The codebase uses shorthand model names mapped to explicit IDs in `claude_cli.py` and `orchestrator/main.py`:
- `"opus"` -> `"claude-opus-4-6"`
- `"sonnet"` -> `"claude-sonnet-4-5-20250929"`
- `"haiku"` -> `"claude-haiku-4-5-20251001"`

### Sensitive Env Var Filtering

`SENSITIVE_ENV_VARS` in `claude_cli.py` lists vars filtered from Claude CLI subprocess (DISCORD_TOKEN, API keys, etc). Claude uses proxy API instead of direct Discord access.

### Claude Settings Hooks

`config/claude_settings.json` defines three hooks:
- **PreToolUse (Task)**: Blocks the Task tool, forces Wendy to use beads (`bd`) instead
- **PostToolUse (Read)**: Reminds about `analyze_file` after Read tool (for media files)
- **Stop**: Checks journal before stopping (`journal_stop_check.sh`)

### Path Module

All filesystem paths are centralized in `bot/paths.py`. Use its functions (`channel_dir()`, `beads_dir()`, `session_dir()`, `journal_dir()`, etc.) instead of constructing paths manually.

## Channel Configuration

Channels are configured via `WENDY_CHANNEL_CONFIG` env var (JSON array):

```json
[
  {"id":"123","name":"chat","mode":"chat"},
  {"id":"456","name":"coding","mode":"full","model":"opus","beads_enabled":true}
]
```

- `mode`: `"full"` (coding capabilities) or `"chat"` (restricted file access)
- `model`: Override model shorthand (`"opus"`, `"haiku"`, or default `"sonnet"`)
- `beads_enabled`: Enable background task queue for this channel
- Webhook messages use the channel's configured model (same as regular messages)

## Environment Variables

### Bot Service
- `DISCORD_TOKEN` - Discord bot token
- `WENDY_CHANNEL_CONFIG` - JSON array of channel configs
- `WENDY_DB_PATH` - SQLite database path (default: `/data/wendy/shared/wendy.db`)
- `SYSTEM_PROMPT_FILE` - Path to system prompt (default: `/app/config/system_prompt.txt`)
- `CLAUDE_CLI_TIMEOUT` - Max seconds for CLI response (default: 300)
- `JOURNAL_NUDGE_INTERVAL` - Invocations between journal nudges (default: 10)
- `MESSAGE_LOGGER_GUILDS` - Comma-separated guild IDs for message archival

### Proxy Service
- `GEMINI_API_KEY` - Google Gemini API key for `/api/analyze_file`

### Orchestrator
- `ORCHESTRATOR_CONCURRENCY` - Max concurrent agents (default: 1)
- `ORCHESTRATOR_POLL_INTERVAL` - Seconds between task checks (default: 30)
- `ORCHESTRATOR_AGENT_TIMEOUT` - Max agent runtime in seconds (default: 1800)
- `ORCHESTRATOR_NOTIFY_CHANNEL` - Discord channel for task notifications

## Secrets Management

Secrets live at `/srv/secrets/wendy/` on the Orange Pi (read-only mount at `/secrets/`):
- `bot.env` - DISCORD_TOKEN, WENDY_CHANNEL_CONFIG, deploy tokens
- `sites.env` / `games.env` - service-specific tokens

Runtime secrets (writable by Wendy): `/data/wendy/secrets/runtime.json`

## Deployment

```bash
# From the cee-wtf repo root:
tools/deploy wendy-bot               # Full deploy
tools/deploy --restart-only wendy-bot # Restart without re-uploading
tools/deploy --rollback wendy-bot     # Revert to previous version
```

Secrets are never overwritten by deployments.

## Git Repository Note

This is a **separate git repo** from cee-wtf. Don't add wendy-bot files to cee-wtf's git index.

```bash
git add -A && git commit -m "message"
GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519" git push origin main
```

## Live E2E Testing

```bash
# Test webhook for #coding channel (triggers Wendy with channel's configured model)
# Webhook URL is in .claude/CLAUDE.md (gitignored secrets file)
curl -s -X POST $WENDY_TEST_WEBHOOK \
  -H "Content-Type: application/json" \
  -d '{"content":"Wendy, quick test - what is 2+2?"}'
```

## Database Queries

```bash
docker exec wendy-bot python scripts/query_db.py --schema
docker exec wendy-bot python scripts/query_db.py "SELECT message_id, author_nickname, timestamp FROM message_history ORDER BY timestamp DESC LIMIT 10"
```

## Debugging Wendy Sessions

Always SSH to the Orange Pi to inspect session transcripts directly:

```bash
# List session files for a channel
ssh ubuntu@100.120.250.100 "docker exec wendy-bot ls -lt /root/.claude/projects/-data-wendy-channels-{CHANNEL}/ | head -10"

# Search for specific patterns in the active session
ssh ubuntu@100.120.250.100 "docker exec wendy-bot grep -c 'pattern' /root/.claude/projects/-data-wendy-channels-{CHANNEL}/{SESSION_ID}.jsonl"

# Check the active claude_settings.json in the container
ssh ubuntu@100.120.250.100 "docker exec wendy-bot cat /app/config/claude_settings.json"
```

## Important Workflow Rules

- **Changes to wendy-bot require deployment.** After finishing a feature, explicitly tell the user whether it has been deployed or is local-only, and ask if they want to deploy.
- **Local testing is not deployment.** Validating JSON or running a script on the dev machine does not mean it's live. Always verify changes in the running container after deploy.
- **Deploy command:** `tools/deploy wendy-bot` (from cee-wtf repo root)
