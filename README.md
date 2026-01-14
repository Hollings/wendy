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
Discord <-> wendy-bot <-> Claude Code CLI
                |
                v
            Proxy API
                |
          +-----+-----+
          |           |
    wendy-sites   wendy-games
     (static)    (multiplayer)
```

## Components

### wendy-bot/
The Discord bot and proxy API.
- `bot/` - Discord bot using discord.py, invokes Claude Code CLI
- `proxy/` - FastAPI service for sandboxed messaging and deployment
- `config/` - System prompt defining Wendy's personality

### wendy-sites/
Static site hosting at wendy.monster. Also serves the Brain Feed - a real-time visualization of Wendy's Claude Code session.
- `backend/` - FastAPI app for site deployment and brain feed WebSocket
- Handles tarball uploads, serves static files

### wendy-games/
Multiplayer game server manager at wendy.monster/game/*.
- `manager/` - FastAPI app that spawns and manages game containers
- `runtime/` - Deno helper library for game servers
- Each game runs in its own Docker container with WebSocket support

## Setup

1. Clone and configure:
```bash
# Bot
cp deploy/.env.example deploy/.env

# Sites
cp wendy-sites/deploy/.env.example wendy-sites/deploy/.env

# Games
cp wendy-games/deploy/.env.example wendy-games/deploy/.env

# Edit each .env with your tokens
```

2. Run with Docker:
```bash
# Start all services
cd deploy && docker compose up -d
cd ../wendy-sites/deploy && docker compose up -d
cd ../wendy-games/deploy && docker compose up -d
```

## Wendy's Creations

Wendy deploys her projects to wendy.monster:
- **wendy.monster/** - Brain feed (watch Wendy think in real-time)
- **wendy.monster/landing/** - Her personal landing page
- **wendy.monster/game/** - Multiplayer games she's built

Her code lives in `/data/wendy/wendys_folder/` inside the container.

## License

MIT
