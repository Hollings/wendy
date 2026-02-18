# Wendy v2 -- Comprehensive Codebase Review

**Scope:** All source files under `wendy/` (13 modules), `tests/` (6 test files, 79 tests), and `deploy/` (Dockerfile, docker-compose.yml, entrypoint.sh). Evaluated against `DESIGN.md`.

**Overall Assessment:** This is a well-executed rewrite that successfully collapses 3 services (~7800 lines) into a single-process architecture (~2400 lines) while preserving feature parity. The code is clear, well-structured, and follows the design doc closely. There are, however, several concrete bugs (one critical), concurrency issues with raw SQLite connections bypassing the StateManager, a file descriptor leak in the task runner, and a security exposure from binding the internal API to 0.0.0.0 instead of localhost.

---

## 1. High-Level Code Flow Analysis

### Startup Flow

1. `__main__.py:main()` reads `DISCORD_TOKEN`, instantiates `WendyBot`, calls `bot.run(token)`.
2. `WendyBot.__init__()` parses `WENDY_CHANNEL_CONFIG` via `config.parse_channel_configs()`, builds the channel whitelist, creates shared directories.
3. `setup_hook()` fires before `on_ready`:
   - Copies scripts to `/data/wendy/` via `setup_wendy_scripts()`
   - Seeds fragment files via `fragment_setup.setup_fragments_dir()`
   - Sets the bot reference and channel configs on `api_server` (module-level globals)
   - Starts the aiohttp HTTP server on port 8945 (binds to `0.0.0.0`)
   - Starts `watch_notifications` task loop (5-second interval)
   - Starts emoji caching
   - Starts `TaskRunner.run()` as a background asyncio task
4. `on_ready()` ensures per-channel directories exist and copies claude settings.

### Message Handling Flow

1. `on_message(message)` filters: own messages, DMs, guild-wide logging, channel whitelist.
2. For whitelisted channels: caches to SQLite, saves attachments, checks `_should_respond()`.
3. If no active generation for the channel, creates a `GenerationJob` and spawns `_generate_response()` as an asyncio task.
4. `_generate_response()` calls `prompt.build_system_prompt()` then `cli.run_cli()`.
5. `run_cli()` resolves session (get/create), builds CLI command, spawns `claude` subprocess, writes nudge prompt to stdin, reads streaming JSON from stdout.
6. Claude CLI runs autonomously, calling `curl http://localhost:8945/api/send_message` and `curl http://localhost:8945/api/check_messages/{channel_id}`.
7. `api_server.handle_send_message()` checks for new-message interrupts, then calls `channel.send()` via discord.py directly -- no outbox, no polling.
8. On CLI completion, session stats are updated, truncation is checked, and if `new_message_pending` is set, a new generation cycle starts.

### Background Tasks Flow

1. `TaskRunner.run()` polls every 30 seconds for beads-enabled channels.
2. Reads `issues.jsonl` via `bd ready --unassigned`, claims tasks, spawns agent subprocesses (forking from Wendy's current session).
3. Monitors agents for completion/timeout/external closure.
4. Writes notifications to SQLite, which `watch_notifications` picks up and injects as synthetic messages.

### System Prompt Assembly (9 layers)

1. Base system prompt from file -> 2. Person fragments -> 3. Channel/common fragments -> 4. Tool instructions template -> 5. Journal section with nudge state -> 6. Beads active task warning -> 7. Thread context -> 8. Topic fragments (keyword-matched) -> 9. Anchor fragments.

---

## 2. Bugs

### BUG 1 (Critical): `get_permissions_for_channel` returns identical permissions for "chat" and "full" modes

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/cli.py`, lines 153-179

The `if mode == "chat":` branch (lines 158-164) and the `else:` branch (lines 165-171) produce the exact same `allowed` string. In v1, "chat" mode was more restrictive -- it limited Bash access and had tighter Write/Edit restrictions. Here, both modes grant identical tool permissions, which means "chat" mode channels have no meaningful restrictions compared to "full" mode.

This defeats the purpose of the mode system entirely. Claude in a "chat" channel gets full Bash access, full Read access, and the same Write/Edit scope as "full" mode.

```python
# Lines 158-171: both branches are identical
if mode == "chat":
    allowed = (
        f"Read,WebSearch,WebFetch,Bash,"
        f"Edit(//data/wendy/channels/{channel_name}/**),..."
    )
else:
    allowed = (
        f"Read,WebSearch,WebFetch,Bash,"
        f"Edit(//data/wendy/channels/{channel_name}/**),..."
    )
```

### BUG 2 (High): `on_raw_message_edit` updates messages for non-whitelisted channels

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/discord_client.py`, lines 182-194

The edit handler checks `MESSAGE_LOGGER_GUILDS` but not the channel whitelist. When `MESSAGE_LOGGER_GUILDS` is empty (a valid config), the condition on line 186 is `if MESSAGE_LOGGER_GUILDS and ...` which evaluates to `False` (because the set is falsy), so the function falls through and runs `update_message_content` for ANY guild the bot is in. The UPDATE is harmless if the original message was never inserted, but the logic is inverted from the intended behavior -- it should skip updates for non-logged guilds, not for logged ones.

### BUG 3 (Medium): Synthetic message ID collision after restart

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/discord_client.py`, lines 497-514

The synthetic ID formula is `9_000_000_000_000_000_000 + int(time.time_ns() // 1000) + _synthetic_counter`. After a bot restart, `_synthetic_counter` resets to 0. If a notification arrives within the same microsecond window as a pre-restart notification, the IDs collide and `INSERT OR IGNORE` silently drops the message.

### BUG 4 (Medium): `_check_closed_tasks` can KeyError if agent was already cleaned up

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/tasks.py`, line 354

The line `del self.agents[task_id]` assumes the key exists. While single-threaded asyncio makes concurrent modification impossible in practice, using `self.agents.pop(task_id, None)` would be more defensive and consistent with how `_check_agents` handles removal via a `finished` list + separate deletion loop.

---

## 3. Top 5 Weaknesses

### W1. API Server Binds to 0.0.0.0 -- Exposing Internal API to the Network

**Severity:** High
**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/api_server.py`, line 744

```python
site = web.TCPSite(runner, "0.0.0.0", port)
```

The design doc states: "The trust boundary is the HTTP API" and "HTTP localhost is the natural interface." But the server binds to all interfaces, not just localhost. Since the container runs with `network_mode: host`, this exposes the internal API on all host interfaces, including the Tailscale interface (100.x.x.x).

Any machine on the Tailscale network can call `/api/send_message` to send arbitrary Discord messages as Wendy, `/api/deploy_site` to deploy arbitrary sites, or `/api/analyze_file` to proxy requests through the Gemini API key. There is no authentication on any endpoint.

**Impact:** Unauthenticated remote message sending and deployment through the exposed API.

**Fix:** Change to `web.TCPSite(runner, "127.0.0.1", port)`. This is a one-character fix.

---

### W2. Multiple Raw SQLite Connections Bypass StateManager

**Severity:** High
**Files:**
- `/mnt/c/Users/jhol/wendy-v2/wendy/api_server.py`, lines 81 and 289
- `/mnt/c/Users/jhol/wendy-v2/wendy/fragments.py`, line 317
- `/mnt/c/Users/jhol/wendy-v2/wendy/discord_client.py`, line 380

The DESIGN.md states: "One file: state.py. One schema definition. One StateManager class." But four other locations open raw `sqlite3.connect()` calls:

1. `api_server.check_for_new_messages()` (line 81)
2. `api_server.handle_check_messages()` (line 289)
3. `fragments.get_recent_messages()` (line 317)
4. `discord_client._has_pending_messages()` (line 380)

These raw connections do not set WAL mode (the PRAGMA is only in StateManager._get_conn), creating potential write contention. They also duplicate query logic that should live in StateManager methods.

**Impact:** Under load, raw connections without WAL can hit "database is locked" errors. The StateManager's timeout and WAL configuration are bypassed.

**Fix:** Add query methods to `StateManager` for these four use cases and route all database access through the singleton.

---

### W3. File Descriptor Leak in TaskRunner Agent Spawning

**Severity:** High
**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/tasks.py`, lines 269-282

```python
log_file = open(log_path, "w")
# ... write header ...
proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=log_file,
    ...
)
```

The `log_file` file descriptor is opened but never closed. It is passed to `create_subprocess_exec` as `stdout`, but the file object reference is lost when `_spawn_agent` returns. The `RunningAgent` dataclass does not store it.

Each agent spawn leaks one file descriptor. With `CONCURRENCY=3` and regular task processing, this will eventually exhaust the container's file descriptor limit.

Additionally, if `create_subprocess_exec` raises an exception on line 277, the `log_file` is leaked since there is no `try/finally` or context manager.

**Impact:** Gradual file descriptor exhaustion leading to `OSError: [Errno 24] Too many open files`.

**Fix:** Store `log_file` on `RunningAgent` and close it in `_check_agents` when the agent finishes. Or use a separate cleanup mechanism.

---

### W4. No Authentication on Internal API Endpoints

**Severity:** Medium
**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/api_server.py`

None of the API endpoints verify that requests come from a legitimate Claude CLI subprocess. Combined with W1 (binding to 0.0.0.0), this means any process on the host or network can call any endpoint without authentication.

Even when bound to localhost, there is no shared secret between the bot and Claude CLI. A malicious tool execution within Claude CLI could call the API directly to bypass normal flow.

**Impact:** No defense-in-depth at the API trust boundary.

**Fix:** Generate a random bearer token at startup, pass it to Claude CLI via the filtered environment, and validate it on every request.

---

### W5. `prompt.py` Imports from `cli.py` -- Inverted Dependency

**Severity:** Medium
**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/prompt.py`, line 24

```python
from .cli import TOOL_INSTRUCTIONS_TEMPLATE
```

The DESIGN.md shows `prompt.py` importing from `paths, fragments, config` -- NOT from `cli.py`. The code comment in `cli.py` line 40 says: "Lives here in Phase 1, moves to prompt.py in Phase 2." Phase 2 has been implemented but the move was never completed. This creates a fragile import graph -- if `cli.py` ever imports from `prompt.py` (as the design intends), it will create an actual circular import.

**Impact:** Fragile import structure that will break if the natural dependency direction is followed.

**Fix:** Move `TOOL_INSTRUCTIONS_TEMPLATE` from `cli.py` to `prompt.py`.

---

## 4. Top 5 Strengths

### S1. Single-Process Architecture Is a Major Simplification

The collapse from 3 Docker services (~7800 lines) into a single asyncio process (~2400 lines) is the headline achievement. The `api_server.handle_send_message` calling `channel.send()` directly eliminates the entire outbox system (file writes, polling, parsing). This removes latency, failure modes, and hundreds of lines of code. The deployment model is also simpler: one container, one process, same volumes.

### S2. Clean Module Boundaries and Import Hierarchy

The actual import graph closely matches the DESIGN.md. Leaf modules (`paths.py`, `models.py`, `config.py`) have zero internal imports. State management is centralized in a singleton. No circular imports exist in practice. The deferred imports in `discord_client.py` (e.g., `from .prompt import build_system_prompt` inside a method) show awareness of import ordering issues. This is a codebase that can be understood by reading the module list.

### S3. Robust Session Recovery and Truncation

The session lifecycle handles difficult edge cases well:
- Corrupt sessions trigger automatic retry with `force_new_session=True` (`cli.py` lines 511-523)
- Truncation walks backwards to find a clean cutoff point, never splitting mid-tool-result (`sessions.py` lines 137-145)
- Thread sessions fork from parent sessions with proper ID extraction from stream events (`cli.py` lines 532-536)
- The `_count_discord_messages` function parses nested tool_result JSON to count actual Discord messages, not raw session entries

### S4. New-Message Interrupt System Is Correctly Implemented

The interrupt detection (`check_for_new_messages`, api_server.py lines 68-125) correctly:
- Filters synthetic messages (ID >= 9e18) to avoid false interrupts from task notifications
- Updates `last_seen` atomically
- Returns new messages with the interrupt response so Claude can re-read context
- Provides clear guidance to prevent Claude from mentioning the interrupt to users
- Is positioned as the first check in `handle_send_message` before any Discord calls

### S5. Fragment System Safely Sandboxes User-Provided Code

The `execute_select()` function in `fragments.py` (lines 121-138) wraps user-provided select snippets in a function with restricted builtins (`_SAFE_BUILTINS`). It limits snippet length to 2000 chars, catches all exceptions, and defaults to `False` on failure. The frontmatter parsing uses `yaml.safe_load`. The fragment matching logic (`matches_context`) handles all five types correctly with clear precedence rules.

---

## 5. Deviations from DESIGN.md

### D1. `message_logger.py` Module Missing

DESIGN.md line 151 lists `message_logger.py` as a dedicated module. It does not exist. Message caching is handled inline in `discord_client.py:_cache_message()` (lines 214-236). Functionally equivalent but deviates from the documented structure.

### D2. `models.py` Dataclasses Are Partially Unused

`ChannelConfig` and `ConversationMessage` dataclasses are defined but never instantiated. Channel configs are passed as raw dicts. Only `SessionInfo` and `Notification` are used (by `state.py`).

### D3. API Server Binds to 0.0.0.0 Instead of Localhost

DESIGN.md says: "HTTP localhost is the natural interface." The code binds to `0.0.0.0`. See W1.

### D4. TOOL_INSTRUCTIONS_TEMPLATE Lives in cli.py, Not prompt.py

DESIGN.md shows tool instructions as part of prompt assembly. The code comment says "moves to prompt.py in Phase 2." Phase 2 was implemented but the move was not completed. See W5.

### D5. No Dedicated Message Logger Module

The DESIGN.md module structure shows `message_logger.py` with dedicated responsibilities. In v2, guild-wide message archival is handled by inline methods in `discord_client.py` with no dedicated module or test coverage.

---

## 6. Additional Issues

### Concurrency / Async

**C1. `_get_media_duration` blocks the event loop** (`api_server.py` lines 567-586)

`subprocess.run` with `timeout=30` is synchronous. During this call, no other requests or Discord events can be processed. Should use `asyncio.create_subprocess_exec`.

**C2. StateManager threading primitives are unnecessary** (`state.py` lines 29-30)

`threading.local()` and `threading.Lock()` serve no purpose in a single-process asyncio application. They are harmless but add confusion about the concurrency model.

### Security

**S1. Game logs endpoint passes unsanitized name to URL** (`api_server.py` line 519)

```python
f"{WENDY_GAMES_URL}/api/games/{name}/logs"
```

If `name` contains URL-encoded characters, they are forwarded to the upstream service.

### Missing Error Handling

**E1. Unhandled ValueError in query parameter parsing** (`api_server.py` line 270)

```python
limit = min(int(request.query.get("limit", "10")), MAX_MESSAGE_LIMIT)
```

Passing `limit=abc` throws an unhandled `ValueError` resulting in a 500 error. Same on line 273.

### Test Coverage Gaps

- **No tests for `api_server.py`** -- the trust boundary module has zero test coverage
- **No tests for `discord_client.py`** -- thread resolution, generation lifecycle, notification routing untested
- **No tests for `tasks.py`** -- agent spawning, timeout, closed-task detection untested
- **No tests for `fragment_setup.py`** -- file seeding logic untested
- **No tests for `cli.run_cli()`** -- the most complex function in the codebase is untested

---

## 7. Recommended Next Steps (Prioritized)

1. **[Critical] Fix the bind address** in `api_server.py` line 744: change `"0.0.0.0"` to `"127.0.0.1"`. One-line fix for the most serious security issue.

2. **[Critical] Fix `get_permissions_for_channel`** in `cli.py` lines 153-179: implement actual differentiation between "chat" and "full" modes. The v1 code had real differences here -- chat mode restricted Bash and Write access.

3. **[High] Fix the file descriptor leak** in `tasks.py`: store `log_file` on `RunningAgent`, close it when the agent finishes.

4. **[High] Route all SQLite access through StateManager**: add methods for the four raw-connection use cases in `api_server.py`, `fragments.py`, and `discord_client.py`.

5. **[High] Move `TOOL_INSTRUCTIONS_TEMPLATE`** from `cli.py` to `prompt.py` (as the code comment and design doc both intended).

6. **[Medium] Make `_get_media_duration` async**: replace `subprocess.run` with `asyncio.create_subprocess_exec`.

7. **[Medium] Add test coverage for `api_server.py`**: test `_validate_attachment_path`, `check_for_new_messages`, and the interrupt logic.

8. **[Medium] Add input validation** for query parameters in `handle_check_messages` (lines 270, 273).

9. **[Low] Clean up `models.py`**: either adopt the dataclasses or remove the unused ones.

10. **[Low] Update DESIGN.md** to reflect actual module structure.
