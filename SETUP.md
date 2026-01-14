# Wendy Bot Setup

Wendy is a Discord bot powered by Claude CLI for subscription-based billing.

## Architecture

```
wendy-bot/
├── bot/                    # Discord bot
│   ├── __main__.py        # Entry point
│   ├── wendy_cog.py       # Main bot cog
│   ├── wendy_outbox.py    # Message sending via outbox
│   ├── claude_cli.py      # Claude CLI integration
│   └── conversation.py    # Data structures
├── proxy/                  # API proxy for Wendy's tools
│   └── main.py            # FastAPI app
├── scripts/               # Shell scripts for Wendy
│   ├── deploy.sh          # Site/game deployment
│   ├── query_db.py        # Database queries
│   └── game_logs.sh       # Game server logs
├── config/
│   └── system_prompt.txt  # Wendy's personality
└── deploy/
    ├── Dockerfile
    ├── docker-compose.yml
    └── .env.example
```

## Quick Start

1. **Create Discord Application**
   - Go to https://discord.com/developers/applications
   - Create new application named "Wendy"
   - Go to Bot section, create bot
   - Enable MESSAGE CONTENT INTENT
   - Copy the token

2. **Deploy to Server**
   ```bash
   ./deploy.sh
   ```

3. **Configure**
   ```bash
   ssh 100.120.250.100
   cd /srv/wendy-bot/deploy
   cp .env.example .env
   # Edit .env with your Discord token and channel IDs
   ```

4. **Start Services**
   ```bash
   docker compose up -d
   ```

5. **Login to Claude CLI**
   ```bash
   docker exec -it wendy-bot claude login
   ```
   Follow the prompts to authenticate.

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Discord bot token |
| `WENDY_WHITELIST_CHANNELS` | Comma-separated channel IDs |
| `WENDY_DEPLOY_TOKEN` | Token for wendy-sites service |
| `WENDY_GAMES_TOKEN` | Token for wendy-games service |

### Volumes

- `wendy_data` - Persistent data at `/data/wendy`
  - `wendys_folder/` - Wendy's personal workspace
  - `outbox/` - Message queue
  - `attachments/` - Downloaded attachments
  - `session_state.json` - Claude CLI session tracking
- `claude_config` - Claude CLI config at `/root/.claude`

## Logs

```bash
# Bot logs
docker logs -f wendy-bot

# Proxy logs
docker logs -f wendy-proxy
```

## Troubleshooting

### OAuth Token Expired
If Wendy says her token expired:
```bash
docker exec -it wendy-bot claude login
```

### Session Issues
To reset a channel's session:
```
!reset
```

### Check Session Stats
```
!context
```
