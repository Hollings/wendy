# Wendy Avatar

3D visualization of Wendy's Claude Code sessions. Watch her work in real-time.

## Vision

Wendy as a lo-fi coder girl in her room. She sits at a desk, works on her computer, and you watch through the window. The monitor shows what she's doing - reading messages, writing code, running commands.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        wendy-avatar                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ stream.js│→ │ states.js│→ │ wendy.js │  │monitor.js│        │
│  │          │  │          │  │          │  │          │        │
│  │ WebSocket│  │  State   │  │Character │  │ Screen   │        │
│  │ events   │  │ Machine  │  │ Control  │  │ Content  │        │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘        │
│        │              │             │             │             │
│        └──────────────┴─────────────┴─────────────┘             │
│                              │                                   │
│                        ┌─────┴─────┐                            │
│                        │  main.js  │                            │
│                        │  Three.js │                            │
│                        │   Scene   │                            │
│                        └───────────┘                            │
└─────────────────────────────────────────────────────────────────┘
                               │
                               │ WebSocket
                               ▼
                    wendy-sites /ws/brain
                               │
                               │ tails
                               ▼
                    /data/wendy/stream.jsonl
```

## States

Based on analysis of 5000 real events from Wendy's sessions:

| State | Frequency | Trigger | Monitor Shows | Animation |
|-------|-----------|---------|---------------|-----------|
| `idle` | - | No session / result event | Screensaver | Head down, breathing |
| `waking` | 428 | system.init | Screen on | Lifts head |
| `thinking` | 719 | text block | - | Thought bubble + TTS |
| `check_messages` | 427 | Bash check_messages | Chat window | Reading |
| `send_message` | 546 | Bash send_message | Chat typing | Typing + TTS |
| `terminal` | ~300 | Other Bash | Terminal | Watching/typing |
| `read_file` | ~100 | Read (non-image) | Code viewer | Reading |
| `read_image` | ~38 | Read (image) | Image | Looking |
| `editing` | 114 | Edit | Diff view | Typing |

### MVP States (Phase 1)

Simplified loop covering ~70% of activity:

```
     ┌──────────────────────────────────────────┐
     │                                          │
     ▼                                          │
  [IDLE] ──system.init──→ [WAKING]              │
     ▲                        │                 │
     │                        ▼                 │
     │              [CHECK_MESSAGES]            │
     │                        │                 │
     │                        ▼                 │
     │                 [WORKING] ←──────┐       │
     │                     │            │       │
     │          ┌──────────┼────────────┤       │
     │          ▼          ▼            ▼       │
     │    [THINKING]  [TERMINAL]  [EDITING]     │
     │          │          │            │       │
     │          └──────────┼────────────┘       │
     │                     │                    │
     │                     ▼                    │
     │              [SEND_MESSAGE]              │
     │                     │                    │
     │                     ▼                    │
     └────result─────── [DONE] ─────────────────┘
```

## File Structure

```
wendy-avatar/
├── index.html          # Entry point
├── README.md           # This file
├── src/
│   ├── main.js         # Three.js scene setup, render loop
│   ├── stream.js       # WebSocket connection to brain feed
│   ├── states.js       # State machine logic
│   ├── monitor.js      # Monitor content renderer (DOM)
│   ├── wendy.js        # Character controller (placeholder → model)
│   └── tts.js          # Text-to-speech (stub)
├── assets/
│   └── models/         # GLB files (future)
└── styles/
    └── main.css        # Monitor content styles
```

## Deployment

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Internet                                 │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                    ┌───────┴───────┐
                    │    Caddy      │  (Lightsail VPS)
                    │  SSL + Proxy  │
                    └───────┬───────┘
                            │ Tailscale
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
    ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
    │ wendy-avatar  │ │ wendy-sites   │ │  wendy-bot    │
    │   :8915       │ │   :8910       │ │               │
    │ Static files  │ │ Brain WS API  │ │ Claude CLI    │
    └───────────────┘ └───────┬───────┘ └───────┬───────┘
                              │                 │
                              │    tails        │ writes
                              ▼                 ▼
                        /data/wendy/stream.jsonl
```

**Data Flow:**
1. wendy-bot runs Claude CLI, writes events to `stream.jsonl`
2. wendy-sites tails the file, broadcasts via WebSocket
3. wendy-avatar (browser) connects to wendy-sites `/ws/brain`
4. Avatar renders 3D scene based on events

### Deploy to Orange Pi

```bash
# From wendy-avatar directory
chmod +x deploy.sh
./deploy.sh
```

Or manually:
```bash
cd /mnt/c/Users/jhol/cee-wtf/services/wendy-bot/wendy-avatar
tar -czf /tmp/wendy-avatar.tar.gz index.html src/ styles/ assets/ deploy/
scp /tmp/wendy-avatar.tar.gz ubuntu@100.120.250.100:/tmp/
ssh ubuntu@100.120.250.100 "mkdir -p /srv/wendy-avatar && tar -xzf /tmp/wendy-avatar.tar.gz -C /srv/wendy-avatar && cd /srv/wendy-avatar/deploy && docker compose -p wendy-avatar up -d --build"
```

### Add to Caddy (Lightsail)

```bash
ssh -i ~/.ssh/lightsail-west-2.pem ec2-user@44.255.209.109
sudo vi /etc/caddy/Caddyfile
```

Add:
```
avatar.wendy.monster {
    reverse_proxy 100.120.250.100:8915
}
```

Then reload:
```bash
sudo systemctl reload caddy
```

### Ports

| Service | Port | Purpose |
|---------|------|---------|
| wendy-avatar | 8915 | Static file server |
| wendy-sites | 8910 | Brain feed WebSocket |

## Development

### Local Testing

1. Start wendy-sites (for brain feed):
   ```bash
   # On Orange Pi, or run locally with mock data
   ```

2. Start avatar dev server:
   ```bash
   cd wendy-avatar
   python3 -m http.server 8080
   ```

3. Open http://localhost:8080

### Mock Events

For development without live brain feed:
```javascript
// In browser console - single event
window.mockEvent({
  type: 'assistant',
  message: { content: [{ type: 'text', text: 'Thinking...' }] }
});

// Full session simulation
window.mockSession();
```

## Configuration

```javascript
// src/main.js
const CONFIG = {
  BRAIN_HOST: 'wendy.monster',  // Brain feed host
  DEBUG: true,                   // Show debug panel
};
```

## Phases

### Phase 1: MVP (Current)
- [ ] Placeholder geometry for Wendy (cube)
- [ ] State machine responding to events
- [ ] Monitor as DOM overlay showing content
- [ ] Basic state transitions: idle → check → work → send → idle

### Phase 2: Character
- [ ] Rigged humanoid model (Mixamo)
- [ ] Animation clips for each state
- [ ] Smooth transitions between animations

### Phase 3: Polish
- [ ] Room environment (desk, lamp, window)
- [ ] TTS integration
- [ ] Thought bubble for thinking text
- [ ] Idle behaviors (breathing, fidgeting)

### Phase 4: Production
- [ ] Deploy alongside wendy-sites
- [ ] Auth integration
- [ ] Performance optimization
