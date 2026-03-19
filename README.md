# Wendy v2

A Discord bot powered by Claude Code CLI. When someone sends a message in a whitelisted channel, the bot spawns a `claude` subprocess that reads messages, runs tools (shell, files, web), and sends replies back through an internal HTTP API.

The unusual part: Wendy doesn't call the Anthropic API. She runs the `claude` command-line tool as a child process, giving her Claude Code's full capabilities (shell, file I/O, web search, code execution) as native Discord abilities.

See [DESIGN.md](DESIGN.md) for architecture details.

---

## Prerequisites

- Docker + Docker Compose
- A Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- A Claude Code subscription (Max plan or higher)
- A Claude OAuth token: `claude setup-token`

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/Hollings/wendy.git wendy-v2
cd wendy-v2
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Discord bot token from the developer portal |
| `WENDY_CHANNEL_CONFIG` | JSON array of channels (see below) |
| `CLAUDE_CODE_OAUTH_TOKEN` | From `claude setup-token` |
| `WENDY_BOT_NAME` | Your bot's display name (default: `Wendy`) |
| `WENDY_WEB_URL` | Deployment URL if you have a web service (optional) |

**Channel config format:**
```json
[
  {"id": "123456789012345678", "name": "chat", "mode": "chat"},
  {"id": "987654321098765432", "name": "coding", "mode": "full", "model": "opus", "beads_enabled": true}
]
```

Modes: `chat` (limited tools, no task system) or `full` (everything). Models: `opus`, `sonnet`, `haiku`.

### 2. Run locally

```bash
./dev-rebuild.sh
```

This starts the bot with your source directory live-mounted — code changes take effect on restart without rebuilding the image.

### 3. First run

On startup, the bot:
- Seeds default fragment files from `config/claude_fragments/` to the data volume (never overwrites)
- Creates the SQLite database schema
- Connects to Discord and begins listening

No additional setup required. The OAuth token in `.env` handles Claude authentication automatically.

---

## Adding a Channel

1. Get the Discord channel's ID (right-click channel → Copy Channel ID with Developer Mode on)
2. Add it to `WENDY_CHANNEL_CONFIG` in `.env`
3. Optionally create channel-specific fragments (see [Fragment System](#fragment-system))
4. Restart: `./dev-rebuild.sh`

---

## Fragment System

Fragments are markdown files with YAML frontmatter that get assembled into Wendy's system prompt. They live in `config/claude_fragments/` (seeded to the data volume on startup) and can be edited live at `/data/wendy/claude_fragments/`.

See `config/claude_fragments/README.md` for the full schema.

### Fragment types

| Type | Loaded when | Example filename |
|------|-------------|-----------------|
| `common` | Always, all channels | `common_01_communication_style.md` |
| `channel` | Channel ID matches | `1234567890_01_rules.md` |
| `person` | Keywords or author names match | `people/alice.md` |
| `topic` | Keywords match recent messages | `topic_runescape.md` |
| `anchor` | Always, all channels (bottom of prompt) | `anchor_override.md` |

### Creating a channel-specific fragment

```markdown
---
type: channel
order: 5
channel: "YOUR_CHANNEL_ID_HERE"
---

## Rules for This Channel

Whatever instructions apply to this channel...
```

Name it `<channel_id>_05_rules.md` and place it in `config/claude_fragments/`. It will be seeded to the volume on next restart.

### Person files

Drop a `.md` file in `config/claude_fragments/people/` — no frontmatter needed. The filename stem becomes the keyword automatically.

```markdown
# Alice

Alice is a software engineer who likes Rust and climbing.
Discord: alice#1234
```

Person files are part of the [personal pack](#personal-pack) and gitignored — they live on the server's data volume, not in the repo.

---

## Personal Pack

Instance-specific files (person profiles, channel fragments, deployment docs) are managed separately from the base repo via a tarball.

```bash
# Download personal pack from server
./scripts/pack-export.sh [user@server] [output.tar.gz]

# Upload personal pack to a fresh server
./scripts/pack-import.sh [pack.tar.gz] [user@server]
```

**What's in the personal pack:**
- `claude_fragments/people/*.md` — person profiles
- `claude_fragments/<channel_id>_*.md` — channel-specific fragments
- `docs/deployment.md` — instance-specific deployment reference

After importing, restart the bot to pick up the new fragments.

---

## Production Deployment

Wendy is designed to run on a Linux server (tested on Orange Pi 5) via Docker Compose.

```bash
./deploy.sh               # Deploy bot (most common)
./deploy.sh web            # Deploy web service only
./deploy.sh all            # Deploy both
./deploy.sh --restart-only # Restart without uploading/rebuilding
./deploy.sh --logs         # Tail production logs
```

The script rsyncs the repo to `$DEPLOY_HOST` and runs `docker compose up -d --build`. Set `DEPLOY_HOST` (e.g. `export DEPLOY_HOST=user@your-server`).

Production secrets live in an `env_file` directory on the server (never overwritten by deploys).

### Docker volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `wendy_data` | `/data/wendy` | All persistent data: fragments, DB, stream log |
| `claude_config` | `/root/.claude` | Claude CLI session files |

---

## Session Commands

In any configured channel:

| Command | Description |
|---------|-------------|
| `!clear` | Start a fresh session |
| `!resume <id>` | Resume a previous session by ID prefix |
| `!session` | Show current session ID and history |

---

## Development

```bash
# Run tests
python3 -m pytest tests/ -v

# Lint
ruff check .
ruff check --fix .

# Restart dev container (picks up source changes instantly)
./dev-rebuild.sh

# Full image rebuild (after Dockerfile or dependency changes)
./dev-rebuild.sh --build
```

---

## Troubleshooting

**OAuth token expired**

Generate a new token and update `CLAUDE_CODE_OAUTH_TOKEN` in your secrets file:
```bash
claude setup-token
```

**Session issues**

Reset a channel's session:
```bash
docker exec wendy sqlite3 /data/wendy/shared/wendy.db \
  "DELETE FROM channel_sessions WHERE channel_id = YOUR_CHANNEL_ID"
docker compose restart wendy
```

**Query the database**

```bash
docker exec wendy sqlite3 /data/wendy/shared/wendy.db .schema
docker exec wendy sqlite3 /data/wendy/shared/wendy.db \
  "SELECT * FROM message_history ORDER BY timestamp DESC LIMIT 10"
```

**Stale fragments on live volume**

The seeder never overwrites existing files. To force a fragment update on the live volume, edit the file directly:
```bash
docker exec -it wendy nano /data/wendy/claude_fragments/your_fragment.md
```
Changes take effect on the next message (no restart needed).
