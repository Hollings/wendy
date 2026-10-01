# Wendy Brain viewer

A compact technical console for Wendy’s live execution logs and background tasks.

## What the old viewer did

The old React client authenticated with an access code, subscribed to the brain
WebSocket, decoded Claude events, paired tool calls with results, displayed
channel context usage, and filtered activity by channel or background task.
It recognized Discord messages/reactions, thought summaries, shell commands,
file reads/writes/diffs, results, notifications, errors, and turn boundaries.

Its display combined automatically collapsed turns, stacked tool calls,
truncated bodies that expanded on any click, and scrolling based on row count.
A new turn could change the meaning of an existing collapse toggle. Repeated
calls could hide older output. Live updates within a row did not reliably
follow scrolling, and the task sidebar only filtered already-received events.

## What changed

- A flat chronological timeline retains distinct calls and turn boundaries.
  Thin dividers separate turns, including when filters hide system events.
- The event log uses aligned time, source/type, content/result, and state columns.
  Shell commands are visible directly. Errors are marked in the stream.
- Event types and tools have distinct label colors and left-edge markers, with
  matching inspector accents in both themes. Failed events retain error styling.
- The detail inspector opens beside the log when selected; closing it returns
  the space to the log. It exposes session, attempt, and tool-call identifiers.
- Compact channel context usage and active tasks stay in the source rail.
  Channels with loaded events appear by default; Show all channels exposes
  historical entries. Repeated names include an ID suffix for disambiguation.
  The interface defaults to a dark theme and retains a light theme option.
- Clicking anywhere in an event row opens details; **Inspect** remains available
  for keyboard navigation. Selecting or copying text does not open the inspector.
- The inspector shows complete prose, commands, file diffs, results, and parsed
  incoming messages. The Event data tab includes the normalized event record
  and original Claude envelopes for both the call and its paired result.
- Search includes tool inputs, results, and execution identifiers. Channel/task selection and event
  type are independent, explicit filters. Errors include failed paired tools.
- Pause freezes the reading snapshot; incoming events continue buffering.
  Scrolling away from the end pauses automatically. Resume returns to live.
- The browser retains up to 1,200 parsed events. A paused snapshot and selected
  detail survive buffer trimming; this viewer is not a complete session archive.
- A task table preserves wtask phases, models, source queues, and status notes.
  Selecting a task loads its latest saved attempt as well as live activity.
- Task logs use bounded 256 KiB reads with file identity/cursor tracking.
  Old logs are tailed, partial lines wait for completion, and retries reset the
  cursor even if the replacement file is larger. Missing event timestamps are
  labeled as saved log entries rather than invented exact times.
- Reconnects preserve the existing buffer and deduplicate UUIDs. Status polling
  retains last-known data on errors. Expired authentication returns to sign-in.
- New sign-ins retain only the expiring token, not the access code. Existing
  stored codes can be exchanged once for compatibility, then removed.
- Responsive channel navigation, mobile detail reading, keyboard controls,
  light/dark appearances, and explicit empty/loading/error states.

This remains a read-only viewer. It does not start tasks or send Discord messages.

## Development

Install frontend dependencies with `npm ci`, then run `npm run dev`.
The Vite server proxies `/api/brain` and `/ws/brain` to
`http://127.0.0.1:8000`; set `BRAIN_API_URL` to change that target.

For a visual preview with fictional data, open `/replay.html` on the Vite server.
It renders the same components as production. The production build includes only
`index.html`; the replay entry is not shipped as a served route.

To exercise the real auth/API/WebSocket handlers with local fixtures, install
`services/web/requirements.txt` in a virtual environment and run, from the repo:

```sh
python services/web/brain-ui/dev/preview_server.py
```

This server binds only to 127.0.0.1:8000. Open the normal Vite root page and use
the fictional access code `preview`. Fixtures stay in `.venv/brain-preview/`;
production volumes and credentials are never used. Writing a `disconnect`
file there simulates a lost connection. Restarting the fixture server resets
its sample stream and task logs.

## Local demo with live Wendy data

From the repository root on Windows:

```powershell
.venv/Scripts/python.exe services/web/brain-ui/dev/live_demo.py
```

Open **http://127.0.0.1:5174/** and enter the local demo code **live**.
This runs Vite on 5174 and a loopback-only read API on 8001. Both ports can be
changed with `--port` and `--api-port`; use `--no-ui` to run only the API.
The regular fictional preview on 5173 can remain running independently.

Requirements: frontend dependencies, `services/web/requirements.txt`, Node.js,
OpenSSH, and access to `DEPLOY_HOST` from the repository `.env` (or environment).
The private network connection to the server must be available.

The demo sends the checkout's Brain **read functions** to an in-memory Python
process in the live `wendy-web` container over SSH. It polls every two seconds,
replays up to 1,200 recent stream frames, and tails new agent log records.
Selecting a task loads its latest saved attempt using the same bounded,
rotation-aware reader intended for production. Original session payloads,
task phases, models, and attempt IDs remain available.

The demo tests this checkout's viewer and readers with live data, independently
of the deployed web image. No remote code files are installed, production
services restarted, tasks run, or messages sent by the demo.
Production access codes and tokens are not copied. The local access code
only authenticates this read-only demo, whose token is held in memory.
Live data stays in memory; the demo does not create an event archive.

SSH failures close the local stream so the viewer shows reconnection state;
the reader reconnects automatically. Stop the launcher with **Ctrl+C** to
stop the local API, Vite child process, and SSH reader. A launcher restart
requires signing in again because its local token changes.

## Verification commands

```sh
# From services/web/brain-ui:
npm test
npm run build

# From the repo root:
python -m pytest services/web/tests/ -q
ruff check services/web/
```

The JavaScript tests cover source/attempt isolation, tool pairing, repeat
visibility, search/error filtering, history deduplication, thinking updates, and
task phases. Python tests cover metadata preservation, UTF-8/partial lines,
bounded reads, log rotation, completion, and path validation.

Browser checks should include valid/invalid sign-in; task selection and saved
logs; text search; error filters; explicit inspection; pause across new events
and reconnect; scrolling; narrow mobile widths; dark appearance; and sign-out.
