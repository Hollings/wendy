# Wendy Games - Deployment Guide

## Overview

Wendy Games allows Wendy (Discord AI bot) to deploy multiplayer game backends with WebSocket support to wendy.monster.

## Architecture

```
Wendy (Claude CLI)
      |
      | calls deploy_game.sh <game-name>
      v
wendy_proxy (hollingsbot) ─── adds GAMES_TOKEN ───> wendy-games manager
      |                                                    |
      v                                                    v
POST /api/deploy_game                              Docker: wendy-game-{name}
                                                          |
                                                          v
                                                   https://wendy.monster/game/{name}/
                                                   wss://wendy.monster/game/{name}/ws
```

## Deployment Steps

### 1. Deploy wendy-games service

```bash
cd services/wendy-games
./deploy.sh
```

### 2. Generate and configure deploy token

On Orange Pi:
```bash
# Generate token
python3 -c "import secrets; print(secrets.token_hex(32))"

# Add to wendy-games
vi /srv/wendy-games/deploy/.env
# Set: DEPLOY_TOKEN=<generated-token>

# Restart manager
cd /srv/wendy-games/deploy
docker compose up -d --force-recreate manager
```

### 3. Add token to hollingsbot

On the hollingsbot server:
```bash
vi /path/to/hollingsbot/.env
# Add: WENDY_GAMES_TOKEN=<same-token>

# Restart wendy_proxy
docker compose restart wendy_proxy
```

### 4. Update Caddy on Lightsail

SSH to Lightsail and update `/etc/caddy/Caddyfile`:

```
wendy.monster {
    # Game backends at /game/gamename/ (including WebSocket)
    handle /game/* {
        reverse_proxy 100.120.250.100:8920
    }

    # Everything else goes to static sites
    handle {
        reverse_proxy 100.120.250.100:8910
    }
}
```

The manager at port 8920 handles:
- HTTP requests: proxied to the correct game container
- WebSocket (`/game/{name}/ws`): proxied to the correct game container
- Management API (`/api/*`): direct handling

## Testing

1. Create a test game:
```bash
mkdir -p /data/wendy/wendys_folder/game
cat > /data/wendy/wendys_folder/game/server.ts << 'EOF'
import { createGameServer } from "/app/lib.ts";

const game = createGameServer({
  onConnect: (player) => {
    console.log("Player joined:", player.id);
    game.broadcast({ type: "join", id: player.id });
  },
  onMessage: (player, msg) => {
    console.log("Message:", msg);
    game.broadcast({ type: "msg", from: player.id, data: msg });
  },
  onDisconnect: (player) => {
    game.broadcast({ type: "leave", id: player.id });
  }
});
EOF
```

2. Deploy:
```bash
./deploy_game.sh test
```

3. Test WebSocket:
```javascript
const ws = new WebSocket("wss://wendy.monster/game/test/ws");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
ws.onopen = () => ws.send(JSON.stringify({type: "hello"}));
```

## Port Allocation

- Manager: 8920
- Games: 8921-8940 (20 games max)
