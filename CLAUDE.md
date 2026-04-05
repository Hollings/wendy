# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Wendy v2

Discord bot where each channel gets a persistent Claude CLI session. A single Python process replaces the old bot + proxy + orchestrator trio.

**GitHub:** github.com/Hollings/wendy

---

## Development Commands

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run a single test file
python3 -m pytest tests/test_fragments.py -v

# Run a single test
python3 -m pytest tests/test_fragments.py::test_sticky_parsing -v

# Lint
ruff check .
ruff check --fix .
```

### Dev Container (`./dev-rebuild.sh`)

The source directory is live-mounted into the container — code changes take effect on restart without rebuilding the image.

```bash
./dev-rebuild.sh              # Restart wendy (picks up code changes instantly)
./dev-rebuild.sh web          # Restart web service
./dev-rebuild.sh all          # Restart both

./dev-rebuild.sh --build      # Full image rebuild + recreate (after Dockerfile/dep changes)
./dev-rebuild.sh --build web
./dev-rebuild.sh --build all
```

---

## Architecture: Core Request Flow

```
Discord message
  → WendyBot.on_message()           [discord_client.py]
  → _generate_response()
  → run_cli()                       [cli.py]
  → claude CLI subprocess (-p, --resume SESSION_ID, --output-format stream-json)
      ↑ stdin: nudge prompt ("you have new messages, call check_messages first")
      ↓ stdout: stream-json events (parsed but mostly ignored -- Wendy responds via API)
  → Claude uses shell helper commands that hit the internal HTTP API on localhost:8945
      msg "hello"                   → POST /api/send_message → bot sends Discord message
      react MSG_ID fire             → POST /api/send_message → bot adds reaction
      curl .../check_messages/:id   → GET  → bot returns recent messages from SQLite
```

Claude CLI runs **headless** (`-p`). Wendy's responses are never captured from stdout — she must use the `msg` command (which hits the internal API). The nudge prompt injected via stdin is the only user input Claude CLI receives each turn.

---

## Session Model

Each channel has a persistent Claude CLI session (a JSONL file under `/root/.claude/projects/...`). Sessions are tracked in SQLite (`channel_sessions` table).

- **New session**: `claude --session-id UUID` — creates a fresh JSONL
- **Resume**: `claude --resume UUID` — continues existing conversation
- **Thread fork**: `claude --resume PARENT_UUID --fork-session` — copies parent context into a new session for the thread

Session lifecycle: `sessions.py` manages create/resume/reset. `state.py` handles SQLite persistence. On `!clear`, a new UUID is created; the old one is archived in `session_history`.

---

## Prompt Assembly (9 layers)

Built fresh each invocation in `prompt.py:build_system_prompt()`:

1. `config/system_prompt.txt` — base personality + tool docs (supports `<!-- FULL_ONLY_START -->..<!-- FULL_ONLY_END -->` blocks stripped in `chat` mode)
2. Channel fragments (`common_*.md` + `{channel_id}_*.md`)
3. Person fragments (contextual, based on who's talking)
4. `TOOL_INSTRUCTIONS_TEMPLATE` — how to use `msg`/`react` commands and the internal API
5. Journal section — lists journal files, emits nudge if overdue
6. Beads warning — active background task count
7. Thread context (if in a thread)
8. Topic fragments (keyword-triggered, sticky)
9. Anchor fragments (behavioral reinforcement, always last)

---

## Fragment System (`wendy/fragments.py`)

Fragments are `.md` files in `/data/wendy/claude_fragments/` with YAML frontmatter:

| Type | Loaded when |
|------|-------------|
| `common` | Always, all channels |
| `anchor` | Always, always last in prompt |
| `channel` | When `channel` field matches current channel ID |
| `person` | When `user_ids` matches message authors, or `keywords`/`match_authors` matches |
| `topic` | Keyword match in recent messages; stays loaded for `sticky` turns after keywords stop |

**`people/` subdir**: `.md` files auto-loaded as `person` fragments. If a file has no valid frontmatter, the filename stem becomes the keyword and `match_authors: true` is set automatically. Wendy writes new person files here.

**Topic stickiness**: once a topic fragment's keywords match, it stays loaded for `TOPIC_STICKY_TURNS` (default 8) more turns. Per-topic state tracked in `.topic_state.json` in the channel dir. The `sticky` frontmatter field overrides per-fragment.

**`select` field**: arbitrary Python expression evaluated against recent messages for conditional loading.

**Seeding vs runtime**: `fragment_setup.py` copies `config/claude_fragments/` to `/data/wendy/claude_fragments/` on startup but **never overwrites** existing files. This means Wendy can edit fragments at runtime and her changes persist. However, repo updates to existing fragments won't propagate automatically. Use `scripts/sync-fragments.sh` to compare and resolve differences between repo and server.

---

## Internal API (`wendy/api_server.py`, port 8945)

Wendy calls this from inside the CLI subprocess. For sending messages and reactions, shell helper scripts (`bin/msg`, `bin/react`) wrap the HTTP calls -- Wendy uses those instead of raw curl. The helpers read `WENDY_CHANNEL_ID` and `WENDY_PROXY_PORT` from the subprocess environment (set by `_build_cli_env()` in `cli.py`).

| Command / Endpoint | Purpose |
|---------------------|---------|
| `msg "text"` | Send Discord message (wraps POST `/api/send_message`) |
| `react MSG_ID emoji` | Add reaction (wraps POST `/api/send_message` with actions) |
| `GET /api/check_messages/:channel_id` | Fetch recent messages from SQLite |
| `POST /api/deploy_site` | Deploy static site tarball to wendy-web |
| `POST /api/deploy_game` | Deploy Deno game server tarball |
| `POST /api/analyze_file` | Gemini file analysis |
| `GET /api/emojis` | Search custom server emojis |

The `wendy-web` service (port 8910) hosts static sites, game containers, and the brain feed. It shares the `wendy_data` Docker volume (same SQLite DB and stream log).

---

## Bot Commands (in Discord)

| Command | Description |
|---------|-------------|
| `!clear` | Reset the current Claude session (archives old, starts fresh UUID) |
| `!resume <id>` | Resume a previous session by ID prefix |
| `!session` | Show current session ID, start time, turn count, and token usage |
| `!version` | Show the running git commit |
| `!system` | Upload the assembled system prompt as a text file (useful for debugging) |

---

## Module Import Hierarchy

```
paths.py, models.py, config.py    (leaf — no internal imports)
         |
         v
state.py                          (imports: paths, models)
         |
         v
fragments.py                      (imports: paths, state)
fragment_setup.py                 (imports: paths)
sessions.py                       (imports: paths, state, config)
         |
         v
prompt.py                         (imports: paths, fragments, config)
enrichment.py                     (no internal imports)
cli.py                            (imports: paths, sessions, prompt, state, config)
tasks.py                          (imports: paths, sessions, cli, state, config)
         |
         v
api_server.py                     (imports: state, paths, config)
discord_client.py                 (imports: cli, api_server, tasks, state,
                                            fragment_setup, enrichment, config)
         |
         v
__main__.py                       (imports: discord_client)
```

No circular imports. `paths.py`, `models.py`, and `config.py` are leaf modules — import them freely.

---

## Concurrency Model

`discord_client.py` runs a single asyncio event loop. Each channel has at most one `GenerationJob` (wraps an asyncio Task running `run_cli`). When a message arrives while CLI is running, `new_message_pending = True` is set; the task's `finally` block starts a new generation if pending.

**WENDY interrupt**: typing `WENDY` (all caps) cancels the current task (which kills the CLI subprocess via `CancelledError` → `proc.kill()`), inserts a synthetic system message, and starts a fresh CLI invocation on the same session. The active job dict entry is replaced *before* `task.cancel()` so the old task's `finally` doesn't restart itself.

**Synthetic messages**: notifications (task completions, webhooks) are inserted into SQLite with IDs starting at `9_000_000_000_000_000_000` so they appear in `check_messages` responses and Wendy sees them naturally.

---

## Beads Background Tasks (`wendy/tasks.py`)

**`bd` is an external Go binary** ([github.com/steveyegge/beads](https://github.com/steveyegge/beads)) installed in the Docker image -- it is NOT part of this repo. It provides a lightweight issue tracker backed by a `.beads/` directory (SQLite + JSONL). Wendy's code interfaces with bd exclusively through subprocess calls; it never imports or modifies bd's internals.

**How Wendy connects to bd:**
- `TaskRunner._run_bd()` in `tasks.py` executes `bd` subcommands (init, ready, update, show, close, comment) as the wendy user via `asyncio.create_subprocess_exec`
- The CLI subprocess (Wendy in Discord) runs `bd create`, `bd list`, etc. directly from the shell -- the `BEADS_DIR` env var (set by `_build_cli_env()` in `cli.py`) tells bd where the channel's `.beads/` directory is
- Both paths must run as the wendy user (UID 1000) so file ownership stays consistent

`bd create "description"` forks the current Claude session (`--fork-session`) to run a background agent. The `TaskRunner` polls `beads_dir/issues.jsonl` and emits `task_completion` notifications when tasks finish. Up to `ORCHESTRATOR_CONCURRENCY` (default 3) agents run concurrently.

---

## Hooks (`config/claude_settings.json` → copied to each channel's `.claude/settings.json`)

Active hooks:
- **PreToolUse `Task`**: blocked — Wendy must use `bd` instead
- **PostToolUse `Read`**: `remind_analyze_file.sh`
- **PostToolUse `Bash`** (async): `log_bash_tool.sh`
- **Stop**: `journal_stop_check.sh` (15 turns + 3h min interval), `prompt_bookkeeping.sh` (25 turns + 2h min interval)

`stop_hook_active = true` prevents infinite block loops.

---

## Key Paths (runtime, inside Docker)

| Path | Contents |
|------|----------|
| `/data/wendy/channels/{name}/` | Channel workspace (Wendy's files, attachments, journal) |
| `/data/wendy/claude_fragments/` | All fragment files including `people/` subdir |
| `/data/wendy/shared/wendy.db` | SQLite: `message_history`, `channel_sessions`, `notifications` |
| `/root/.claude/projects/` | Claude CLI session JSONL files |
| `/app/config/` | System prompt, hooks, settings (read-only at runtime) |

---

## Server Access

`DEPLOY_HOST` is defined in `.env` (e.g. `root@100.x.x.x`). Always read it from there -- do NOT go hunting through SSH keys or `~/.ssh/config` to find the server.

See `config/docs/infrastructure.md` for architecture details.

Secrets live on the server at `/srv/secrets/wendy/` (mounted read-only, never overwritten by deploys):

| File | Contents |
|------|----------|
| `bot.env` | `DISCORD_TOKEN`, `WENDY_CHANNEL_CONFIG`, deploy tokens, `GEMINI_API_KEY` |
| `sites.env` | `DEPLOY_TOKEN`, `BRAIN_ACCESS_CODE`, `BRAIN_SECRET` |

A local copy with actual values is in `.env` (gitignored). See `.env.example` for the template.

---

## Deployment

```bash
./deploy.sh               # Deploy bot (most common)
./deploy.sh web            # Deploy web service only
./deploy.sh all            # Deploy both
./deploy.sh --restart-only # Restart without uploading/rebuilding
./deploy.sh --logs         # Tail production logs
```

The script rsyncs the repo to `$DEPLOY_HOST` and runs `docker compose up -d --build`. Requires `rsync` (Linux/macOS). On Windows, manually `scp` changed files to `/srv/wendy-v2/` on the server, then `ssh $DEPLOY_HOST "cd /srv/wendy-v2/deploy && docker compose up -d --build wendy"`.

**Fragment sync**: after deploying code, run `scripts/sync-fragments.sh` to diff repo fragments against the server's live copies and resolve conflicts interactively. Wendy edits fragments at runtime, so repo and server can diverge.

---

## Services

| Container | Port | Purpose |
|-----------|------|---------|
| `wendy` | host | Discord bot + internal API (port 8945) |
| `wendy-web` | 8910 | Static sites + game servers + brain feed |
| `wendy-game-{name}` | 8921+ | Individual game containers (spawned by wendy-web) |

---

## Local Development Setup

1. Copy `.env.example` to `.env` and fill in credentials.

2. Create the games bind mount directory (required — named volumes don't work for game mounts in Docker Desktop):
   ```bash
   mkdir -p /tmp/wendy-games-dev
   ```

3. Build the Deno game runtime image (once):
   ```bash
   docker compose -f deploy/docker-compose.dev.yml --profile build up runtime-builder
   ```

4. Start services:
   ```bash
   docker compose -f deploy/docker-compose.dev.yml up --build
   ```

5. Log in to Claude CLI inside the bot container (once):
   ```bash
   docker compose -f deploy/docker-compose.dev.yml exec wendy claude login
   ```

Note: `/tmp/wendy-games-dev` is not persistent across reboots. For a permanent dev location, edit `docker-compose.dev.yml` and change the bind mount source.

---

## Adding a Discord Channel

Edit `WENDY_CHANNEL_CONFIG` in `bot.env` on the server, then restart.

```json
[
  {"id":"123...","name":"chat","mode":"chat"},
  {"id":"456...","name":"coding","mode":"full","model":"sonnet","beads_enabled":true}
]
```

Modes: `"chat"` (limited file access) or `"full"` (full coding tools). Models: `"opus"`, `"sonnet"`, `"haiku"`.

---

## Personal Pack

Instance-specific files (person profiles, channel-specific fragments, deployment docs) are managed separately from the repo via a tarball.

```bash
# Download personal pack from server
./scripts/pack-export.sh [user@server] [output.tar.gz]

# Upload personal pack to a (fresh) server
./scripts/pack-import.sh [pack.tar.gz] [user@server]
```

Contains: `claude_fragments/people/*.md`, `claude_fragments/<channel_id>_*.md`, `docs/deployment.md`. These are gitignored and live on the data volume.

---

## Troubleshooting

### CLI auth failing silently

The CLI uses `CLAUDE_CODE_OAUTH_TOKEN` from the env. **Do NOT use `claude login`** — it writes `.credentials.json` which takes priority over the env var and expires after ~2 days, causing silent failures. The entrypoint deletes `.credentials.json` on startup as a safeguard.

To rotate the token: update `CLAUDE_CODE_OAUTH_TOKEN` in `/srv/secrets/wendy/bot.env` on the server, then restart.

Note: `CLAUDE_CODE_OAUTH_TOKEN` is in `SENSITIVE_ENV_VARS` (stripped from the CLI subprocess env for security), then explicitly re-added by `_build_cli_env()` in `cli.py`. If the token isn't reaching the CLI, check that code path.

### Reset a session

```bash
docker exec wendy sqlite3 /data/wendy/shared/wendy.db \
  "DELETE FROM channel_sessions WHERE channel_id = 1234567890"
docker compose restart wendy
```

### Query the database

```bash
docker exec wendy sqlite3 /data/wendy/shared/wendy.db .schema
docker exec wendy sqlite3 /data/wendy/shared/wendy.db \
  "SELECT * FROM message_history ORDER BY timestamp DESC LIMIT 10"
```

### Check session transcripts

```bash
docker exec wendy ls -lt /root/.claude/projects/-data-wendy-channels-coding/ | head -10
```

---

## Volumes

| Volume | Mount | Contents |
|--------|-------|----------|
| `wendy_data` | `/data/wendy` | All persistent bot data (channels, fragments, DB, stream log) |
| `claude_config` | `/root/.claude` | Claude CLI session files |
| `wendy-sites_sites_data` | `/data/sites` | Deployed static sites |
| bind: `HOST_GAMES_DIR` | `/data/games` | Deployed game server files |

`wendy_data` is shared between `wendy` and `wendy-web` — both read/write the same SQLite DB.

---

## Environment Variables

### wendy (bot)

| Variable | Purpose | Default |
|----------|---------|---------|
| `DISCORD_TOKEN` | Discord bot token | required |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude CLI auth token (`claude setup-token`) | required |
| `WENDY_CHANNEL_CONFIG` | JSON array of channel configs | required |
| `WENDY_DB_PATH` | SQLite path | `/data/wendy/shared/wendy.db` |
| `SYSTEM_PROMPT_FILE` | System prompt path | `/app/config/system_prompt.txt` |
| `WENDY_PROXY_PORT` | Internal API port | `8945` |
| `CLAUDE_CLI_TIMEOUT` | Max CLI runtime (seconds) | `300` |
| `ORCHESTRATOR_CONCURRENCY` | Max concurrent beads agents | `3` |
| `ORCHESTRATOR_POLL_INTERVAL` | Seconds between beads task polls | `30` |
| `ORCHESTRATOR_AGENT_TIMEOUT` | Max beads agent runtime (seconds) | `1800` |
| `JOURNAL_NUDGE_INTERVAL` | Invocations between journal nudges | `10` |
| `WENDY_WEB_URL` | URL of wendy-web service | `https://wendy.monster` |
| `WENDY_BOT_NAME` | Bot display name (used in prompts) | `Wendy` |
| `WENDY_BOT_USER_ID` | Bot's Discord user ID (for filtering own messages) | `0` |
| `WENDY_DEPLOY_TOKEN` | Token for site deploys | — |
| `WENDY_GAMES_TOKEN` | Token for game deploys | falls back to `WENDY_DEPLOY_TOKEN` |
| `GEMINI_API_KEY` | Gemini API for file analysis | — |
| `MESSAGE_LOGGER_GUILDS` | Guild IDs for full message archival | — |
| `WENDY_DEV_MODE` | Set to `1` to enable dev mode | — |

### wendy-web (sites + games + brain)

| Variable | Purpose | Default |
|----------|---------|---------|
| `DEPLOY_TOKEN` | Auth for site/game deploys | required |
| `GAMES_TOKEN` | Auth for game deploys | falls back to `DEPLOY_TOKEN` |
| `BRAIN_ACCESS_CODE` | Code users type to access brain feed | required |
| `BRAIN_SECRET` | HMAC signing secret for brain tokens | required |
| `WENDY_DB_PATH` | SQLite path (shared with wendy) | `/data/wendy/shared/wendy.db` |
| `SITES_DIR` | Static sites directory | `/data/sites` |
| `GAMES_DIR` | Game files directory | `/data/games` |
| `HOST_GAMES_DIR` | Host path for game volume mounts | (set in compose) |
| `BASE_PORT` | First port for game containers | `8921` |
| `MAX_GAMES` | Max simultaneous games | `20` |
| `DOCKER_NETWORK` | Network game containers join | `wendy_web` |
| `BASE_URL` | Public URL base | `https://wendy.monster` |
| `WEBHOOK_SECRET` | HMAC secret for GitHub webhooks | — |
