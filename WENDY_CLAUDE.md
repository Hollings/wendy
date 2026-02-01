# Wendy Bot - Claude Code Guide

This document covers Wendy's architecture, deployment, and development. For task system usage, see `config/BD_USAGE.md`.

## Overview

Wendy is a Discord bot powered by Claude Code. She runs as multiple Docker services on an Orange Pi home server.

**GitHub:** github.com/Hollings/wendy (separate repo from cee-wtf monorepo)

## Architecture

```
Discord <-> Bot (Claude Code) <-> Proxy <-> Discord API
                |
                v
         Orchestrator --> Background Agents (tasks)
                |
                v
           wendy-sites (wendy.monster)
           wendy-games (game servers)
```

### Services

| Service | Port | Purpose |
|---------|------|---------|
| **bot** | - | Main Claude Code session, responds to Discord messages |
| **proxy** | 8945 | Bridges bot to Discord API, manages message queue |
| **orchestrator** | - | Spawns background agents for bd tasks |
| **wendy-sites** | 8910 | Hosts wendy.monster sites + Brain dashboard |
| **wendy-games** | 8920 | WebSocket game server manager |

### Data Volumes

- `wendy_data` → `/data/wendy/` - Wendy's working directory, beads DB, logs
- `claude_config` → `/root/.claude/` - Claude Code config and session data

## Git Repository

**IMPORTANT:** This is a SEPARATE git repo from cee-wtf.

```bash
# Push changes (from services/wendy-bot directory)
cd /mnt/c/Users/jhol/cee-wtf/services/wendy-bot
git add -A && git commit -m "message"
GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519" git push origin main
```

**Do NOT:**
- Add wendy-bot files to the cee-wtf git index
- Run `git reset --hard` in cee-wtf if wendy-bot files are staged (will delete them)

## Secrets Management

Secrets are stored in `/srv/secrets/wendy/` on the Orange Pi, completely separate from the deployment directory. Deployments never touch secrets.

```
/srv/secrets/wendy/           # Static secrets (read-only mount at /secrets/)
  bot.env       # DISCORD_TOKEN, WENDY_CHANNEL_CONFIG, deploy tokens
  sites.env     # DEPLOY_TOKEN, BRAIN_ACCESS_CODE, BRAIN_SECRET
  games.env     # DEPLOY_TOKEN for game deployments

/data/wendy/secrets/          # Runtime secrets (writable)
  runtime.json  # Wendy's own secrets (GitHub PAT, API keys)
```

### First-Time Setup

Run this once on the Orange Pi to initialize secrets:
```bash
ssh ubuntu@100.120.250.100
cd /srv/wendy-bot
./setup-secrets.sh
# Then edit /srv/secrets/wendy/*.env with actual values
```

### Runtime Secrets (Wendy-managed)

Wendy can store her own API keys (GitHub PAT, etc.) using the secrets CLI:

```bash
python3 /data/wendy/secrets.py set github_pat "ghp_xxx"   # Store
python3 /data/wendy/secrets.py get github_pat              # Retrieve
python3 /data/wendy/secrets.py list                        # List keys
python3 /data/wendy/secrets.py delete old_key              # Delete
```

Secrets are stored in `/data/wendy/secrets/runtime.json` and persist across restarts and deployments.

## Deployment

### Deploy All Wendy Services

```bash
# From local machine - secrets are NOT touched
cd /mnt/c/Users/jhol/cee-wtf/services/wendy-bot
./deploy.sh
```

Or manually:
```bash
tar --exclude='node_modules' --exclude='.git' --exclude='__pycache__' \
    -czf /tmp/wendy-bot.tar.gz .
scp /tmp/wendy-bot.tar.gz ubuntu@100.120.250.100:/tmp/
ssh ubuntu@100.120.250.100 "rm -rf /srv/wendy-bot && mkdir -p /srv/wendy-bot && tar -xzf /tmp/wendy-bot.tar.gz -C /srv/wendy-bot"
ssh ubuntu@100.120.250.100 "cd /srv/wendy-bot/deploy && docker compose -p wendy down && docker compose -p wendy up -d --build"
```

### Deploy Individual Components

```bash
# wendy-sites only
tar --exclude='__pycache__' -czf /tmp/wendy-sites.tar.gz wendy-sites
scp /tmp/wendy-sites.tar.gz ubuntu@100.120.250.100:/tmp/
ssh ubuntu@100.120.250.100 "cd /srv/wendy-bot && tar -xzf /tmp/wendy-sites.tar.gz"
ssh ubuntu@100.120.250.100 "cd /srv/wendy-bot/wendy-sites/deploy && docker compose -p wendy-sites down && docker compose -p wendy-sites up -d --build"

# wendy-games only
tar --exclude='node_modules' -czf /tmp/wendy-games.tar.gz wendy-games
scp /tmp/wendy-games.tar.gz ubuntu@100.120.250.100:/tmp/
ssh ubuntu@100.120.250.100 "cd /srv/wendy-bot && tar -xzf /tmp/wendy-games.tar.gz"
ssh ubuntu@100.120.250.100 "cd /srv/wendy-bot/wendy-games/deploy && docker compose -p wendy-games down && docker compose -p wendy-games up -d --build"
```

### Check Status

```bash
ssh ubuntu@100.120.250.100 "docker ps | grep -E 'wendy|orchestrator'"
ssh ubuntu@100.120.250.100 "docker logs wendy-bot --tail 50"
ssh ubuntu@100.120.250.100 "docker logs wendy-orchestrator --tail 50"
```

## Configuration Files

| File | Purpose |
|------|---------|
| `config/system_prompt.txt` | Wendy's personality and behavior rules |
| `config/agent_claude_md.txt` | Context given to background task agents |
| `config/BD_USAGE.md` | Task system documentation (Wendy reads this) |
| `deploy/.env` | Bot environment variables |
| `wendy-sites/deploy/.env` | Sites service env vars (BRAIN_ACCESS_CODE, etc) |

## Environment Variables

### Main Bot (`deploy/.env`)
```
DISCORD_TOKEN=...
DISCORD_WHITELIST_CHANNELS=channel_id1,channel_id2
WENDY_DEPLOY_TOKEN=...  # For deploying to wendy.monster
```

### Wendy Sites (`wendy-sites/deploy/.env`)
```
DEPLOY_TOKEN=...           # Must match WENDY_DEPLOY_TOKEN
BRAIN_ACCESS_CODE=...      # Password for brain.wendy.monster
BRAIN_SECRET=...           # Token signing secret
```

## Key Directories on Orange Pi

```
/srv/wendy-bot/           # Deployed code
/var/lib/docker/volumes/wendy_data/_data/    # Wendy's persistent data
  ├── wendys_folder/      # Projects Wendy creates
  ├── .beads/             # Task queue database
  ├── orchestrator_logs/  # Background agent logs
  └── outbox/             # Files waiting to be sent to Discord
```

## URLs

- **wendy.monster** - Static sites Wendy deploys
- **wendy.monster/game/<name>/** - WebSocket game servers
- **brain.wendy.monster** - Real-time view of Wendy's Claude session

## Troubleshooting

### Bot not responding
```bash
ssh ubuntu@100.120.250.100 "docker logs wendy-bot --tail 100"
ssh ubuntu@100.120.250.100 "docker logs wendy-proxy --tail 100"
```

### Tasks not running
```bash
ssh ubuntu@100.120.250.100 "docker logs wendy-orchestrator --tail 100"
ssh ubuntu@100.120.250.100 "docker exec wendy-bot bd list"
```

### Brain dashboard not connecting
1. Check wendy-sites is running: `docker ps | grep wendy-sites`
2. Check .env has BRAIN_ACCESS_CODE and BRAIN_SECRET set
3. Clear browser localStorage and re-authenticate

### Restart everything
```bash
ssh ubuntu@100.120.250.100 "cd /srv/wendy-bot/deploy && docker compose -p wendy down && docker compose -p wendy up -d"
```

## Database Operations

The SQLite database is at `/data/wendy/wendy.db` inside containers. The container doesn't have `sqlite3` CLI, so use Python.

### View schema
```bash
ssh ubuntu@100.120.250.100 "docker exec wendy-bot python3 -c \"
import sqlite3
conn = sqlite3.connect('/data/wendy/wendy.db')
for row in conn.execute(\\\"SELECT name, sql FROM sqlite_master WHERE type='table'\\\"):
    print(row[0], '-', row[1])
\""
```

### Query message history (read-only)
```bash
# Use the built-in read-only query script
ssh ubuntu@100.120.250.100 "docker exec wendy-bot python scripts/query_db.py --schema"
ssh ubuntu@100.120.250.100 "docker exec wendy-bot python scripts/query_db.py \"SELECT message_id, author_nickname, timestamp FROM message_history ORDER BY timestamp DESC LIMIT 10\""
```

### Delete rows by pattern (without viewing content)
```bash
# Count matches first
ssh ubuntu@100.120.250.100 "docker exec wendy-bot python3 -c \"
import sqlite3
conn = sqlite3.connect('/data/wendy/wendy.db')
count = conn.execute(\\\"SELECT COUNT(*) FROM message_history WHERE content LIKE '%PATTERN%'\\\").fetchone()[0]
print('Rows matching:', count)
\""

# Delete matching rows
ssh ubuntu@100.120.250.100 "docker exec wendy-bot python3 -c \"
import sqlite3
conn = sqlite3.connect('/data/wendy/wendy.db')
cursor = conn.execute(\\\"DELETE FROM message_history WHERE content LIKE '%PATTERN%'\\\")
conn.commit()
print('Deleted', cursor.rowcount, 'row(s)')
\""
```

### Clear all message history
```bash
ssh ubuntu@100.120.250.100 "docker exec wendy-bot python3 -c \"
import sqlite3
conn = sqlite3.connect('/data/wendy/wendy.db')
conn.execute('DELETE FROM message_history')
conn.commit()
print('Cleared message_history')
\""
```

### Delete by message ID
```bash
ssh ubuntu@100.120.250.100 "docker exec wendy-bot python3 -c \"
import sqlite3
conn = sqlite3.connect('/data/wendy/wendy.db')
conn.execute('DELETE FROM message_history WHERE message_id = 123456789')
conn.commit()
\""
```

## Common Maintenance Tasks

### Restart Wendy's session (clears Claude context)
```bash
ssh ubuntu@100.120.250.100 "cd /srv/wendy-bot/deploy && docker compose -p wendy down && docker compose -p wendy up -d"
```

### Restart just the bot (keeps proxy/orchestrator running)
```bash
ssh ubuntu@100.120.250.100 "docker restart wendy-bot"
```

### View recent bot activity
```bash
ssh ubuntu@100.120.250.100 "docker logs wendy-bot --tail 100 -f"
```

### Check disk usage
```bash
ssh ubuntu@100.120.250.100 "du -sh /var/lib/docker/volumes/wendy_data/_data/*"
```

### Clear orchestrator logs
```bash
ssh ubuntu@100.120.250.100 "rm -rf /var/lib/docker/volumes/wendy_data/_data/orchestrator_logs/*"
```
