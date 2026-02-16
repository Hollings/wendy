# Complete Multiplayer Game Deployment Guide

## File Structure

Create your game in `/data/wendy/wendys_folder/<game-name>/` with this structure:

```
<game-name>/
├── server.ts       # Deno backend
└── public/         # Frontend files (automatically served)
    ├── index.html
    └── (any other assets)
```

## Server Code Template (server.ts)

```typescript
import { createGameServer, loadState, saveState } from "/app/lib.ts";

// Define your player data structure
interface Player {
  id: string;
  name: string;
  // ... other player properties
}

// Manage state at module level (NOT in createGameServer options)
const players = new Map<string, Player>();
let playerCounter = 0;

// Optional: Load persisted state
const savedState = await loadState<{ someValue: number }>();

const server = createGameServer({
  // Signature: onConnect(ws, playerId)
  // ws = WebSocket connection, playerId = UUID string
  onConnect(ws, playerId) {
    playerCounter++;
    const player: Player = {
      id: playerId,
      name: `Player ${playerCounter}`,
    };

    players.set(playerId, player);

    // Send to specific player
    server.send(playerId, {
      type: "welcome",
      data: player,
    });

    // Broadcast to everyone (including sender)
    server.broadcast({
      type: "playerJoined",
      player: player,
    });

    // Broadcast to everyone EXCEPT sender
    server.broadcast({
      type: "playerJoined",
      player: player,
    }, playerId);
  },

  // Signature: onMessage(ws, playerId, message)
  // message is automatically parsed from JSON
  onMessage(ws, playerId, message: { type: string }) {
    const player = players.get(playerId);
    if (!player) return;

    if (message.type === "someAction") {
      server.broadcast({
        type: "actionPerformed",
        playerId: playerId,
      });
    }
  },

  // Signature: onDisconnect(playerId)
  onDisconnect(playerId) {
    const player = players.get(playerId);
    if (player) {
      players.delete(playerId);

      server.broadcast({
        type: "playerLeft",
        playerId: playerId,
      });
    }
  },
});

console.log("Game server started!");
```

## Important Notes About Server Code

1. **State Management**: Use module-level variables (NOT `initialState` in options)
2. **Callback Signatures**:
   - `onConnect(ws, playerId)` - NOT `onConnect(playerId, state)`
   - `onMessage(ws, playerId, message)` - message is already parsed JSON
   - `onDisconnect(playerId)`
3. **Broadcasting**:
   - `server.send(playerId, data)` - send to specific player
   - `server.broadcast(data)` - send to all players
   - `server.broadcast(data, excludePlayerId)` - send to all except one
4. **saveState() Warning**: Wrap in try-catch if you use it:
   ```typescript
   try {
     saveState({ data });
   } catch (e) {
     console.error("Failed to save state:", e);
   }
   ```
   Permission errors can crash the entire server!

## Frontend Code (public/index.html)

```javascript
// WebSocket connection - this exact pattern works for deployed games
const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const wsUrl = `${protocol}//${window.location.host}${window.location.pathname}ws`;
const ws = new WebSocket(wsUrl);

ws.onopen = () => {
  console.log('Connected');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // Handle different message types
  switch (data.type) {
    case 'welcome':
      // ...
      break;
  }
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('Disconnected');
  // Optional: auto-reconnect
  setTimeout(() => connect(), 2000);
};

// Send messages to server
ws.send(JSON.stringify({ type: "someAction", data: "..." }));
```

## Deployment

From `/data/wendy/` directory:

```bash
./deploy.sh <game-name>
```

The script auto-detects:
- If `server.ts` exists -> Game server at `wendy.monster/game/<game-name>/`
- WebSocket URL: `wss://wendy.monster/game/<game-name>/ws`
- Files in `public/` folder are automatically served

Result URLs:
- Game: `https://wendy.monster/game/<game-name>/`
- WebSocket: `wss://wendy.monster/game/<game-name>/ws`

## Debugging

Get server logs:
```bash
/data/wendy/game_logs.sh <game-name> 50
```

Common issues to check:
1. Permission errors from `saveState()` -> Wrap in try-catch
2. WebSocket connects but no messages -> Check callback signatures
3. Server crashes after first message -> Check logs for uncaught errors

## Frontend Debugging Tips

Add debug logging to see all messages:

```javascript
ws.onmessage = (event) => {
  console.log('[DEBUG] Received:', event.data);
  try {
    const data = JSON.parse(event.data);
    // ... handle message
  } catch (e) {
    console.error('[ERROR]', e);
  }
};
```

## Testing Multiplayer

1. Deploy the game
2. Open the URL in multiple browser tabs/windows
3. Each tab gets a unique playerId
4. Test message broadcasting by triggering actions in different tabs
5. Check server logs with game_logs.sh
