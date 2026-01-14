# Wendy

A Discord bot powered by Claude Code that can chat, write code, and deploy projects.

## What Wendy Can Do

- **Chat naturally** in Discord channels with a chill, casual personality
- **Write and run code** - from quick scripts to complex multi-file projects
- **Deploy static sites** to wendy.monster
- **Deploy multiplayer games** with WebSocket support
- **Stream her brain** - watch her think in real-time at wendy.monster

## Architecture

```
Discord <-> Bot <-> Claude Code CLI
              |
              v
           Proxy API
              |
        +-----+-----+
        |           |
   wendy-sites  wendy-games
   (static)     (multiplayer)
```

- **bot/** - Discord bot using discord.py, invokes Claude Code CLI for responses
- **proxy/** - FastAPI service providing sandboxed APIs for messaging and deployment
- **config/** - System prompt that defines Wendy's personality and capabilities
- **scripts/** - Deployment utilities for wendy.monster

## Setup

1. Clone and configure:
```bash
cp deploy/.env.example deploy/.env
# Edit deploy/.env with your tokens
```

2. Run with Docker:
```bash
cd deploy
docker compose up -d
```

See [SETUP.md](SETUP.md) for detailed instructions.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Discord bot token |
| `WENDY_WHITELIST_CHANNELS` | Comma-separated channel IDs where Wendy responds |
| `WENDY_DEPLOY_TOKEN` | Token for wendy-sites deployment |
| `WENDY_GAMES_TOKEN` | Token for wendy-games deployment |

## Related Projects

- [wendy-sites](https://github.com/Hollings/wendy-sites) - Static site hosting service
- [wendy-games](https://github.com/Hollings/wendy-games) - Multiplayer game server manager

## License

MIT
