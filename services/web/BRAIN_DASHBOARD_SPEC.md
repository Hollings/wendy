# Wendy Brain Dashboard — API & WebSocket Contract

The Brain dashboard is a real-time, single-page observability surface for Wendy's Claude Code sessions: it streams every stream-json event Claude emits (thoughts, tool calls, tool results, session results) from every channel and every beads background agent, and overlays live status about the bot (context usage, costs, active channels, bead/task state, weekly API quota). It is authenticated with a shared access code, served at `/` off the `wendy-web` service on port 8910, and built as a React SPA that holds one WebSocket open to `/ws/brain` plus periodic REST polls for slower-moving state.

---

## Service layout

- **Backend**: `services/web/main.py` (FastAPI) + `services/web/brain.py` (stream watcher, stats, subagents, beads) + `services/web/auth.py` (HMAC token auth).
- **Frontend** (current impl, for reference only — feel free to replace): `services/web/brain-ui/` Vite + React, built into `services/web/static/brain/`.
- **Data sources** the backend reads from (all inside the container, mounted from the `wendy_data` volume):
  - `/data/wendy/stream.jsonl` — append-only stream of every event Claude CLI emits across all channels. Main firehose. Periodically truncated by the bot to ~5000 lines.
  - `/data/wendy/shared/wendy.db` — SQLite. Tables used: `channel_sessions` (session id, token counts, message count), `thread_registry` (thread display names), `message_history` (for counts), `notifications`.
  - `/data/wendy/shared/beads_snapshot.json` — JSON array of beads written by Wendy's `TaskRunner` each poll cycle. Source of truth for bead list on the dashboard.
  - `/data/wendy/orchestrator_logs/agent_{task_id}_{ts}.log` — stream-json logs from beads agents. Tailed line-by-line, each line is wrapped into an envelope and broadcast over the same WS.
  - `/data/claude/projects/-data-wendy-channels-*/{session_id}/subagents/agent-*.jsonl` — per-`Task`-tool subagent logs. Read on demand via REST.

---

## Authentication

Shared-code + long-lived HMAC token, used for both REST and WS.

- **Server config**: requires env vars `BRAIN_ACCESS_CODE` (the shared code users type) and `BRAIN_SECRET` (HMAC signing key). If either is missing, the dashboard refuses to serve.
- **Token format**: `"{unix_expiry}:{hex_sig}"` where `sig = hmac_sha256(BRAIN_SECRET, f"brain:{expiry}")[:16]`. 30-day lifetime.
- **Client storage**: frontend stores both `brain_token` and the raw `brain_passphrase` in `localStorage` so it can silently re-auth on token expiry or WS 4001 close.
- **REST auth**: `Authorization: Bearer <token>` header, OR `?token=<token>` query string. `401` on failure, `503` if the server is not configured.
- **WS auth**: token is passed as `?token=<token>` query string on the connect URL. Server calls `accept()` then `close(4001)` if invalid.

### `POST /api/brain/auth`

Exchange the shared code for a token.

```http
POST /api/brain/auth
Content-Type: application/json

{ "code": "hunter2" }
```

Response `200`:

```json
{ "token": "1735689600:a1b2c3d4e5f67890" }
```

Errors: `401` invalid code, `503` server not configured.

---

## WebSocket: `GET /ws/brain?token=…`

Single long-lived WS per client. This is the firehose — everything real-time arrives here. All messages are JSON text frames.

### Connection lifecycle

1. Client opens `wss://host/ws/brain?token=...`.
2. Server validates token. On failure: `accept()` then `close(4001, "Invalid or expired token")`.
3. Server enforces a global cap of `MAX_CLIENTS = 100`. Over the cap: `close(4002, "Server at capacity")`.
4. On accept, server sends — in order:
   - One `channels_map` envelope.
   - Up to `MAX_HISTORY = 50` recent stream events read from the tail of `stream.jsonl` (replay, to give new clients immediate context).
5. Live mode: server broadcasts each new line appended to `stream.jsonl`, plus bead list updates and bead agent log lines, to every connected client.
6. Every 60s of no client traffic, server sends `{"type":"ping"}`. Client should reply with the literal string `pong` (plain text, not JSON) to keep the connection healthy.
7. Any client disconnect or send failure is silently dropped.

### Client → server

The only meaningful thing the client sends is a `pong` reply to server pings. Any other messages are ignored.

### Server → client message shapes

All frames are JSON. There are two main categories: **stream events** and **envelopes**. Envelopes are tagged with a `type` field; stream events are not tagged and are identified by the shape of their nested `event`.

#### 1. Stream event (from a Discord channel's Claude session)

```json
{
  "ts": "2026-04-24T18:03:11.412Z",
  "channel_id": "123456789012345678",
  "event": { /* Claude CLI stream-json event: assistant / user / result / system */ }
}
```

Notes:
- `ts` may be an ISO string OR an epoch-ms integer depending on source. Treat both.
- `channel_id` is the Discord channel (or thread) ID as a string. May be absent for some event types, in which case render as "unlabeled".
- `event` is the raw Claude CLI stream-json payload. Same shape as `claude -p --output-format stream-json` output: `{type: "assistant"|"user"|"result"|"system", message?, usage?, subtype?, total_cost_usd?, num_turns?, ...}`.

#### 2. Stream event (from a beads background agent)

Same shape as above but with `bead_id` set and `channel_id` null:

```json
{
  "ts": 1735689600123,
  "bead_id": "abc123",
  "channel_id": null,
  "event": { /* same Claude stream-json event */ }
}
```

#### 3. `channels_map` envelope

Sent once on connect and should be re-fetched from REST after that. Maps channel IDs to display names so chips can be labeled before the first event from that channel arrives.

```json
{
  "type": "channels_map",
  "channels": {
    "123456789012345678": "coding",
    "234567890123456789": "my-thread-name"
  }
}
```

#### 4. `beads_list` envelope

Sent whenever `beads_snapshot.json` changes on disk. Full replacement, not a diff.

```json
{
  "type": "beads_list",
  "beads": [
    {
      "id": "abc123",
      "title": "Fix the reconnect loop",
      "status": "in_progress",
      "created": "2026-04-24T17:00:00Z",
      "updated": "2026-04-24T18:00:00Z"
    }
  ]
}
```

Status values: `"in_progress" | "open" | "closed" | "tombstone"`. Sort order on the server is in-progress → open → closed → tombstone.

#### 5. `ping` envelope

```json
{ "type": "ping" }
```

Client must reply with text frame `pong` within 60s or the socket may be dropped.

### WS close codes the client should handle

| Code | Meaning | Recommended client behavior |
|------|---------|------------------------------|
| 1000 | Normal close | Don't reconnect |
| 4001 | Invalid/expired token | Try silent re-auth with stored passphrase; on failure, show auth screen |
| 4002 | Server at capacity | Show "server full" state; backoff before retry |
| 4003 / 1008 / 3000 | Other auth-ish errors | Same as 4001 |
| Anything else | Transient | Reconnect after ~3s |

---

## REST endpoints

All require `Authorization: Bearer <token>`. All return JSON. Errors use FastAPI default `{"detail": "..."}` shape with appropriate status codes (`401`, `503`, `400`, `404`).

### `GET /api/brain/stats`

Aggregate snapshot. Combines in-memory stats from the stream watcher with a couple of SQLite reads.

```json
{
  "viewers": 3,
  "context_tokens": 123456,
  "context_pct": 61.7,
  "session_cost": 0.4321,
  "last_activity": "2026-04-24T18:03:11.412Z",
  "active_tasks": 2,
  "session_messages": 412,
  "cached_messages": 18234,
  "session_id": "d4f8a1b9",
  "total_input": 12345,
  "total_output": 67890,
  "cache_read": 1234567
}
```

- `context_tokens` = `input_tokens + cache_read_input_tokens` from the most recent assistant event's usage.
- `context_pct` is relative to a 200K window.
- `active_tasks` is tracked by watching `Task` tool_use IDs and matching `tool_result` blocks — it's a best-effort estimate, not authoritative.
- `session_*`, `total_*`, `cache_read` come from the most-recently-used row in `channel_sessions`.
- `cached_messages` is a row count of `message_history`.

Note: the frontend currently does **not** poll this. Most of these numbers can be derived from the WS stream directly, which is what the current UI does. Consider whether a redesign needs this at all.

### `GET /api/brain/channels`

```json
{
  "channels": {
    "123456789012345678": "coding",
    "234567890123456789": "some-thread"
  }
}
```

Same payload as the `channels_map` WS envelope. Frontend polls this every 30s to keep thread names fresh, and immediately when a new `channel_id` appears in the stream.

### `GET /api/brain/beads`

Full list of beads (server-side sorted and trimmed). Fetched on WS connect to populate the sidebar before the first `beads_list` push arrives.

```json
{
  "beads": [
    {
      "id": "abc123",
      "title": "Fix the reconnect loop",
      "status": "in_progress",
      "priority": 1,
      "created": "2026-04-24T17:00:00Z",
      "updated": "2026-04-24T18:00:00Z",
      "labels": ["bug"]
    }
  ]
}
```

Server returns: all active (`in_progress` + `open`) beads, the 10 most recently updated closed ones, and the 5 most recently updated tombstones. The WS `beads_list` envelope uses the same data with a simpler shape (no `priority`/`labels`).

### `GET /api/brain/beads/{task_id}/log?offset=N`

Stream-tail the orchestrator log for a specific bead task. Supports incremental polling via `offset`.

Request: `task_id` must match `[a-zA-Z0-9_-]+` (server rejects anything else with `400`).

Response:

```json
{
  "task_id": "abc123",
  "log": "...new content since offset...",
  "offset": 45678,
  "complete": false
}
```

`complete` flips to true when the log contains `=== TASK COMPLETE ===` or `=== TASK FAILED ===`.

### `GET /api/brain/agents`

List subagent (i.e. `Task` tool) logs for the currently active session.

```json
{
  "agents": [
    {
      "id": "a0f72800",
      "slug": "general-purpose",
      "task": "Investigate the reconnect loop and report back",
      "size": 12345,
      "modified": 1735689600000,
      "path": "/data/claude/projects/.../subagents/agent-a0f72800.jsonl"
    }
  ]
}
```

Sorted newest-first by `modified` (epoch ms).

### `GET /api/brain/agents/{agent_id}?limit=N`

Recent events from a single subagent log. `agent_id` is the ID from the list endpoint.

```json
{
  "agent_id": "a0f72800",
  "events": [
    "{\"ts\":\"...\",\"event\":{...}}",
    "..."
  ]
}
```

Events are returned as **raw JSON strings** (not parsed), newest last, max `limit` (default 50). Each string has the same shape as the WS stream event payload.

---

## Event semantics (what the feed actually shows)

The raw `event` payload inside WS frames is Claude CLI stream-json. The current UI (see `services/web/brain-ui/src/eventUtils.js`) collapses these into a handful of display kinds:

| Raw event | UI kind | Renders as |
|-----------|---------|-----------|
| `assistant` with `thinking` block | `thinking` | Thought bubble |
| `assistant` with `text` block | `thinking` | Thought bubble (treated the same) |
| `assistant` with `tool_use` block | `tool` | Tool call card (icon + tool name + input summary) |
| `assistant` with only `usage` | — | Not rendered; used to update context-%% meter |
| `user` with `tool_result` block | `result` | Tool output card |
| `user` with `text` block | `nudge` | Folded into the preceding `system` card as the nudge text |
| `result` | `session_end` | "Turn ended (N turns)" marker |
| `system` | `system` | Session-init card (holds the nudge text once one arrives) |
| `ping` | — | Skipped |

Known tools with custom icons in the UI: `Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`, `Task`, `TodoWrite`, `WebSearch`, `WebFetch`. Anything else uses a generic fallback.

Context-usage extraction: from any `assistant` event's `message.usage`, compute `context_tokens = input_tokens + cache_read_input_tokens`, percent against a 200K window. The UI tracks this **per `channel_id`** so each channel chip shows its own fill bar.

Deduplication: the frontend keeps a bounded set of synthetic event IDs (`${ts}-${counter}`) to tolerate the replay overlap between the 50-event initial backfill and live events.

---

## Dashboard UX summary (for a redesigner)

The current layout is a two-column SPA:

- **Left (feed column)**: chronological wall of event cards. Connection status pill at top. Auto-scrolls to bottom when the user is within ~80px of bottom; shows a "jump to live" button otherwise. Can be filtered to a single bead (click a bead card) or to a subset of channels (toggle channel chips). Capped at 200 events in memory.
- **Right (sidebar)**: two sections, top-to-bottom:
  1. **Sessions**: one row per channel that has produced events this session, showing channel name (from `channels_map`), time since last activity, and a context-%% fill bar. Clicking toggles the channel on/off in the feed filter.
  2. **Beads**: card grid of active + recent-closed beads. Each card shows title, status, and the most recent event snippet seen for that bead on the WS. Clicking a card focuses the feed onto that bead's events.

Auth flow: unauthenticated users see a passphrase prompt that POSTs to `/api/brain/auth`. The token and the passphrase itself are persisted to `localStorage` — the passphrase is used for silent re-auth when the WS closes with an auth-ish code.

---

## Gotchas worth knowing before a rewrite

- **Stream file truncation**: the bot periodically trims `stream.jsonl` to ~5000 lines. The watcher detects this (file shrank) and jumps to the new end, silently dropping that batch to avoid re-broadcasting history. A naive tail implementation will spam every client with duplicates.
- **Same event can arrive twice**: once in the 50-event replay on connect, once live. The dedup ID trick in `eventUtils.js` handles this — any rewrite needs an equivalent.
- **Timestamps are mixed types**: sometimes ISO strings, sometimes epoch-ms integers, depending on whether the event came from `stream.jsonl` (bot-written) or from an agent log (wrapped envelope uses int ms). Don't assume one format.
- **`active_tasks` in `/api/brain/stats` is approximate**: it's based on matching `Task` tool_use IDs against `tool_result` IDs in the stream. Crashes, truncation, or missed events will drift it.
- **`channels_map` can change mid-session**: new threads spawn, folder names change. Refetch from REST when a new `channel_id` appears, plus a background poll.
- **`MAX_CLIENTS = 100` is a hard cap**: clients get `close(4002)` above it. Not configurable at runtime.
- **Brain assets are mounted at `/assets/...`** from `services/web/static/brain/assets/`. The SPA's `index.html` sits at `services/web/static/brain/index.html` and is served by `GET /`. Any rewrite either keeps those paths or updates `main.py` mounts.
- **The dashboard shares a process and FastAPI app with sites, games, and webhooks** — it's not a standalone service. Auth, CORS, and startup (`brain.start_watcher()`) all live in `main.py`.
