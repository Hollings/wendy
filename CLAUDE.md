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

CI runs lint (ruff) and tests (pytest) on push/PR to main via `.github/workflows/test.yml`.

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

All three core services share a single Dockerfile and two external Docker volumes: `wendy_data` (mounted at `/data/wendy`) and `claude_config` (mounted at `/root/.claude`). These must exist before first deploy.

### Key Data Flow

1. **Discord -> Bot**: `WendyCog.on_message` receives messages, caches to SQLite via `MessageLoggerCog`, saves attachments per-channel
2. **Bot -> Claude CLI**: `ClaudeCliTextGenerator.generate()` spawns `claude` subprocess with `--resume` for session persistence, sends nudge prompt via stdin
3. **Claude CLI -> Discord**: Claude calls `curl` to hit proxy API endpoints (`/api/send_message`, `/api/check_messages`)
4. **Proxy -> Discord**: `WendyOutbox` watches `/data/wendy/shared/outbox/` for JSON files and sends them via discord.py

### Filesystem Layout (on server)

```
/data/wendy/
+-- channels/              # Per-channel workspaces (cwd for Claude CLI)
|   +-- {name}/            # Each channel gets isolated workspace
|       +-- CLAUDE.md      # Wendy's self-editable notes (loaded as system prompt)
|       +-- attachments/   # Downloaded Discord files (per-channel isolation)
|       +-- .claude/       # Claude Code settings (hooks config)
|       +-- .beads/        # Task queue (only if beads_enabled)
|       +-- .current_session  # Session ID for agent forking
+-- shared/
|   +-- outbox/            # Message queue to Discord
|   +-- wendy.db           # SQLite database (all state)
+-- tmp/                   # Scratch space
```

### Session Management

Claude CLI sessions are per-channel with automatic truncation:
- Sessions stored in `/root/.claude/projects/-data-wendy-channels-{name}/` (path encoding replaces `/` with `-`)
- Session state (ID, token counts) tracked in SQLite `channel_sessions` table
- Truncates when Discord messages in session exceed `MAX_DISCORD_MESSAGES` (50) - see `claude_cli.py:_truncate_session_if_needed`

### New Message Interrupts

The proxy prevents stale replies when users send messages while Wendy is thinking:
1. `check_messages` records the last seen message ID in SQLite
2. `send_message` checks if new real messages (not synthetic) arrived since last check
3. If new messages exist, returns them instead of sending (409-like response with guidance)
4. Claude must re-read messages and retry with updated response

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

### Claude Settings Hook

`config/claude_settings.json` blocks the Task tool via a PreToolUse hook, forcing Wendy to use beads (`bd`) for background tasks instead.

### Path Module

All filesystem paths are centralized in `bot/paths.py`. Use its functions (`channel_dir()`, `beads_dir()`, `session_dir()`, etc.) instead of constructing paths manually.

## Channel Configuration

Channels are configured via `WENDY_CHANNEL_CONFIG` env var (JSON array):

```json
[
  {"id":"123","name":"chat","mode":"chat"},
  {"id":"456","name":"coding","mode":"full","model":"opus","beads_enabled":true}
]
```

- `mode`: `"full"` (coding capabilities) or `"chat"` (restricted file access). Affects `--allowedTools`/`--disallowedTools` passed to Claude CLI.
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
- `MESSAGE_LOGGER_GUILDS` - Comma-separated guild IDs for message archival

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
curl -s -X POST https://discord.com/api/webhooks/1463041136888119513/BQ_Yn3TlVGWjo_sAj4z8VvT3B3FjoPc_jCGB_AIU1LpVG4IlZlPDQ4BNvPZr9ZhsDqoL \
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

# Extract tool use patterns (e.g. image reads vs analyze_file)
ssh ubuntu@100.120.250.100 "docker exec wendy-bot grep 'analyze_file\|\.jpg\|\.png' /root/.claude/projects/-data-wendy-channels-{CHANNEL}/{SESSION_ID}.jsonl | tail -30"

# Check if a hook script exists in the running container
ssh ubuntu@100.120.250.100 "docker exec wendy-bot cat /app/config/hooks/remind_analyze_file.sh"

# Check the active claude_settings.json in the container
ssh ubuntu@100.120.250.100 "docker exec wendy-bot cat /app/config/claude_settings.json"
```

## Important Workflow Rules

- **Changes to wendy-bot require deployment.** After finishing a feature, explicitly tell the user whether it has been deployed or is local-only, and ask if they want to deploy.
- **Local testing is not deployment.** Validating JSON or running a script on the dev machine does not mean it's live. Always verify changes in the running container after deploy.
- **Deploy command:** `tools/deploy wendy-bot` (from cee-wtf repo root)
