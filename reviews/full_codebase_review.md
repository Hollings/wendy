# Wendy v2 -- Comprehensive Code Review

**Scope:** All source files across `wendy/`, `services/web/`, `services/runtime/`, `deploy/`, and `tests/` (6,720 lines total).

**Executive Summary:** This is a well-designed rewrite that successfully collapses three services into one, eliminates file-based IPC, and centralizes state management. The architecture is clean, the module boundaries are mostly correct, and the code reads well. However, there are real bugs (truncation counting that never matches the actual API response format, a leaked file handle, duplicate methods that will diverge), some race conditions around SQLite access from async code, and the `services/web/main.py` has grown into a new god module that contradicts the design goal of splitting god modules apart. The test suite covers the right things but misses all async code paths and the entire API server.

---

## 1. High-Level Code Flow Analysis

### Bot startup sequence

1. `__main__.py:main()` reads `DISCORD_TOKEN` from environment, creates `WendyBot`, calls `bot.run()`
2. `WendyBot.__init__()` parses channel configs from env, sets up whitelists
3. `setup_hook()` fires before `on_ready`:
   - Seeds fragment files from `/app/config/` to `/data/wendy/`
   - Sets the discord bot reference in `api_server` (module-level globals)
   - Starts aiohttp API server on `PROXY_PORT` (default 8945)
   - Starts notification watcher loop (5s interval)
   - Starts emoji caching task
   - Creates `TaskRunner` and runs it as an asyncio task
4. `on_ready()` creates channel directories and copies Claude settings

### Message processing flow

1. `on_message()` fires for every guild message from non-self authors
2. Guild-wide message logging fires if guild is in `MESSAGE_LOGGER_GUILDS`
3. Channel whitelist check (`_channel_allowed`) -- allows direct channels + threads of whitelisted channels + @mentions
4. Thread config resolution for thread messages (creates folder, copies CLAUDE.md)
5. Command filtering (messages starting with `!`, `-`, `/` are passed to command processing instead)
6. Channel-specific message caching to SQLite if not already guild-logged
7. Attachment download and save to `channels/{name}/attachments/`
8. `_should_respond()` check (identical logic to `_channel_allowed`)
9. If a generation is already active for this channel, mark `new_message_pending` and return
10. Spawn `_generate_response()` as an asyncio task

### CLI execution flow

1. `_generate_response()` calls `build_system_prompt()` then `run_cli()`
2. `build_system_prompt()` assembles 9 layers: base prompt, persons, channel fragments, tool instructions, journal, beads warning, thread context, topics, anchors
3. `run_cli()` resolves session (create/resume/fork), builds CLI command, spawns subprocess
4. Claude CLI receives a nudge prompt via stdin telling it to `curl check_messages`
5. Claude CLI curls `localhost:8945/api/check_messages/{channel_id}` to read messages
6. Claude CLI curls `localhost:8945/api/send_message` to reply
7. `api_server` checks for new-message interrupts before sending; if new messages exist, returns them instead
8. On completion, session stats are updated and truncation check runs

### Background task flow

1. `TaskRunner.run()` polls every 30s for beads-enabled channels
2. Scans `issues.jsonl` for ready, unassigned tasks
3. Claims task, reads parent session ID from `.current_session` file
4. Forks session with `--resume {id} --fork-session`, spawns Claude CLI agent
5. Monitors for completion (exit code), timeout (30min default), or external close
6. Writes notification to SQLite, which `watch_notifications` picks up and injects as synthetic message

### Web service flow (services/web/)

1. FastAPI app with CORS, serves on port 8910
2. Static site deployment: upload tarball, extract to `/data/sites/{name}/`, serve at catch-all route
3. Game deployment: upload tarball, extract to `/data/games/{name}/`, spawn Docker container with Deno runtime
4. Brain feed: WebSocket streaming of `stream.jsonl` events to authenticated dashboard clients
5. Webhooks: receive GitHub/GitLab webhooks, write notifications to SQLite for Wendy to pick up

---

## 2. Top 5 Weaknesses

### W1. `_channel_allowed()` and `_should_respond()` are identical -- one will silently diverge (Severity: Medium)

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/discord_client.py`, lines 196-212

```python
def _channel_allowed(self, message: discord.Message) -> bool:
    if self.user in message.mentions:
        return True
    if message.channel.id in self.whitelist_channels:
        return True
    if isinstance(message.channel, discord.Thread):
        return message.channel.parent_id in self.whitelist_channels
    return False

def _should_respond(self, message: discord.Message) -> bool:
    if self.user in message.mentions:
        return True
    if message.channel.id in self.whitelist_channels:
        return True
    if isinstance(message.channel, discord.Thread):
        return message.channel.parent_id in self.whitelist_channels
    return False
```

These two methods have byte-for-byte identical implementations. They exist because the v1 design had separate logic for "should we cache/process this message" vs "should we generate a response." In v2, both checks do the same thing. This is a maintenance trap: someone will update one and forget the other. It also means every message that passes the channel check will always trigger a response -- there is no concept of "listen but don't reply," which is something you might want for guild-wide logging channels.

**Suggested fix:** Delete `_should_respond()` and call `_channel_allowed()` in its place. When the semantics actually need to diverge (e.g., listen-only channels), add the second method back with its own logic.

### W2. `get_permissions_for_channel()` chat and full modes have identical code paths (Severity: Medium)

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/cli.py`, lines 153-179

```python
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

The `chat` and `else` (full) branches produce the exact same string. This means the "mode" field in channel config has no effect on permissions whatsoever. If chat mode was supposed to be more restricted (e.g., no Bash), that restriction is not implemented. If both modes are intentionally identical, the branching is dead code that misleads readers.

**Suggested fix:** Either implement the intended difference between chat and full modes, or collapse the branch into a single code path with a comment explaining why modes share the same permissions.

### W3. SQLite accessed from async code via synchronous `sqlite3` with no connection pooling (Severity: High)

**Files:**
- `/mnt/c/Users/jhol/wendy-v2/wendy/state.py`, lines 33-50
- `/mnt/c/Users/jhol/wendy-v2/wendy/api_server.py`, lines 80-125 (raw `sqlite3.connect()` in async handlers)

The `StateManager` uses `threading.local()` for connection storage but is called from `async` functions running on the asyncio event loop. `sqlite3.connect()` and `.execute()` are blocking I/O calls that will block the entire event loop. With WAL mode, brief reads are fast, but any write contention or disk latency will stall Discord message handling.

Worse, `api_server.py` creates its own raw `sqlite3.connect()` connections in `check_for_new_messages()` and `handle_check_messages()` (lines 81, 289), bypassing the `StateManager` entirely. This means two different connection patterns access the same database, and the raw connections do not set WAL mode.

**Impact:** Under load (multiple channels, background tasks writing simultaneously), the event loop blocks on SQLite operations. The raw connections in `api_server.py` may also encounter `SQLITE_BUSY` errors since they use default timeout instead of the 30s timeout configured in `StateManager`.

**Suggested fix:** Use `aiosqlite` for database access from async code, or at minimum run all SQLite operations in a thread pool executor (`asyncio.to_thread()`). Consolidate all database access through `StateManager` to eliminate the duplicate connection pattern in `api_server.py`.

### W4. Leaked file handle in `tasks.py:_spawn_agent()` (Severity: High)

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/tasks.py`, lines 269-282

```python
log_file = open(log_path, "w")
log_file.write(f"Task: {task_id} - {title}\n")
...
log_file.flush()

proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=log_file,
    stderr=asyncio.subprocess.STDOUT,
    cwd=channel_dir(channel_name),
)
```

The file is opened with `open()` but never closed. The file descriptor is passed to the subprocess as stdout, but the parent process also holds a reference. When the subprocess exits, the parent's file descriptor remains open. Over the lifetime of the process with multiple agent spawns, this leaks file descriptors.

Furthermore, if `asyncio.create_subprocess_exec()` raises an exception (e.g., command not found), the file handle leaks immediately with no cleanup.

**Suggested fix:** Wrap in a `try/finally` to close on error, and track the file handle in `RunningAgent` so it can be closed when the agent finishes in `_check_agents()`.

### W5. `services/web/main.py` is a new god module at 857 lines with a duplicate schema definition (Severity: Medium)

**File:** `/mnt/c/Users/jhol/wendy-v2/services/web/main.py`

The design doc explicitly calls out the v1 problem of `proxy/main.py` being 1400 lines with mixed concerns. The new `services/web/main.py` is 857 lines and handles: static site deployment, game container management (Docker operations), game HTTP/WebSocket proxying, brain feed endpoints, webhook reception and processing, avatar static file serving, health checks, and static site catch-all serving.

It also has its own inline `CREATE TABLE IF NOT EXISTS notifications` (lines 717-724), which duplicates the schema from `state.py` -- the exact problem the design doc says to avoid:

```python
conn.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL, source TEXT NOT NULL, channel_id INTEGER,
        title TEXT NOT NULL, payload TEXT,
        seen_by_wendy INTEGER DEFAULT 0, seen_by_proxy INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
""")
```

If the schema changes in `state.py`, this function will not be updated. The web service has no import path to `wendy/state.py` so it cannot share the definition.

**Suggested fix:** Split `services/web/main.py` into focused modules: `sites.py`, `games.py`, `webhooks.py`. The webhook notification writer should use a shared schema definition or at minimum not inline `CREATE TABLE`.

---

## 3. Top 5 Strengths

### S1. The architecture genuinely delivers on the single-process promise

The collapse from 3 services to 1 process is not just cosmetic. File-based IPC is gone. The outbox polling loop is gone. `api_server.py` directly holds a reference to the Discord bot and calls `channel.send()` in-process. This eliminates an entire class of "message stuck in outbox" bugs and reduces latency from hundreds of milliseconds to effectively zero. The `DESIGN.md` laid out a clear vision and the code follows it faithfully.

### S2. Clean module dependency hierarchy with no circular imports

The import structure is a strict DAG: `paths.py`, `models.py`, and `config.py` are leaf modules with zero internal imports. `state.py` imports only from leaves. `fragments.py` and `sessions.py` import from state and leaves. `prompt.py` imports from fragments. `cli.py` imports from sessions and prompt. `discord_client.py` sits at the top. No cycles, no lazy imports to work around cycles (the deferred imports in `discord_client.py` for `fragment_setup` and `cli.setup_channel_folder` are for startup ordering, not cycles). This makes the code genuinely testable, and the test suite proves it -- tests for fragments, config, state, sessions, and prompt all run without needing to mock circular dependencies.

### S3. The fragment system is well-ported and well-tested

The fragment loading pipeline (YAML frontmatter parsing, type-based selection, keyword matching, Python `select` snippets, ordered assembly into prompt sections) is one of the more complex parts of the codebase and it is handled cleanly. The `_SAFE_BUILTINS` sandbox for `exec()` is a reasonable pragmatic choice for a personal bot. The test suite (`test_fragments.py`) covers frontmatter parsing, fragment type filtering, keyword matching, author matching, select execution, error handling, and end-to-end assembly. Fragment seeding (`fragment_setup.py`) is refreshingly simple: copy if not exists, 44 lines, done.

### S4. The new-message interrupt system is cleverly designed

The pattern where `send_message` checks for new messages before actually sending prevents race conditions where Claude would reply to stale context. Lines 161-172 of `api_server.py`:

```python
new_messages = check_for_new_messages(channel_id)
if new_messages:
    return web.json_response({
        "error": "New messages received since your last check. Review them and retry.",
        "new_messages": new_messages,
        ...
    })
```

This is a clean solution to a genuinely hard problem: coordinating a slow AI subprocess with fast-arriving Discord messages. By rejecting the send and returning the new messages, Claude naturally re-reads context and adjusts its response. The synthetic message system for notifications (lines 497-514) extends this same pattern for task completions and webhooks.

### S5. The `auth.py` token system is correct and uses proper cryptographic primitives

`services/web/auth.py` implements HMAC-signed tokens with expiry correctly. It uses `hmac.compare_digest()` for constant-time comparison in both `verify_code()` and `verify_token()`. The token format (`{expiry}:{signature}`) is simple but sufficient. The 30-day expiry is appropriate for a personal dashboard. The module is well-documented with clear docstrings explaining the security model, token format, and environment variable requirements.

---

## 4. Bugs

### BUG 1: Session truncation never triggers because `_count_discord_messages_in_tool_result` does not match the actual API response format

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/sessions.py`, lines 57-66

```python
def _count_discord_messages_in_tool_result(content: str) -> int:
    """Count Discord messages in a check_messages tool result."""
    try:
        data = json.loads(content)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            if "message_id" in data[0] and "author" in data[0]:
                return len(data)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        pass
    return 0
```

This function checks if the parsed JSON is a bare `list` of dicts with `message_id` and `author` keys. But `handle_check_messages()` in `api_server.py` (line 385) returns:

```python
return web.json_response({"messages": messages, "task_updates": task_updates})
```

The tool result content is a **dict** with a `"messages"` key, not a bare list. So `isinstance(data, list)` is always `False`, the function always returns `0`, `_count_discord_messages()` always returns `0`, and `truncate_if_needed()` never triggers.

This means sessions grow unbounded past `MAX_DISCORD_MESSAGES=50`. Context windows fill up with stale history, increasing token usage and degrading response quality over time. The bug is silent -- no error is logged, truncation just never happens.

**Location:** `/mnt/c/Users/jhol/wendy-v2/wendy/sessions.py`, line 60.

### BUG 2: `on_raw_message_edit` silently drops edits for whitelisted channels when `MESSAGE_LOGGER_GUILDS` is set but does not include the guild

**File:** `/mnt/c/Users/jhol/wendy-v2/wendy/discord_client.py`, lines 182-194

```python
async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
    if not payload.guild_id:
        return
    if MESSAGE_LOGGER_GUILDS and payload.guild_id not in MESSAGE_LOGGER_GUILDS:
        return
    ...
```

Message caching at lines 155-157 caches messages from whitelisted channels regardless of `MESSAGE_LOGGER_GUILDS`:

```python
if not MESSAGE_LOGGER_GUILDS or message.guild.id not in MESSAGE_LOGGER_GUILDS:
    self._cache_message(message)
```

If `MESSAGE_LOGGER_GUILDS` is set to guild A, and the bot is also in guild B with whitelisted channels, messages from guild B are cached but their edits are silently dropped (line 186 returns early). Claude will see stale pre-edit content for those messages.

**Location:** `/mnt/c/Users/jhol/wendy-v2/wendy/discord_client.py`, lines 185-186. The edit handler should also check if the message's channel is in the whitelist.

---

## Recommended Next Steps

1. **Fix the truncation counting bug (BUG 1).** This is the highest-impact bug -- sessions will grow unbounded in production, causing increasing latency and token usage. Update `_count_discord_messages_in_tool_result` to handle the `{"messages": [...]}` dict response format. Add a test case that uses the actual response shape.

2. **Eliminate the duplicate SQLite connections in `api_server.py`.** Functions `check_for_new_messages()` and `handle_check_messages()` create raw `sqlite3.connect()` calls that bypass `StateManager`. Route all database access through `StateManager` and consider wrapping calls in `asyncio.to_thread()` to avoid blocking the event loop.

3. **Close the file handle leak in `tasks.py:_spawn_agent()`.** Track the log file handle in `RunningAgent` and close it when the agent completes in `_check_agents()`. Add a `try/finally` to handle exceptions during subprocess creation.

4. **Collapse the dead code branches.** Delete `_should_respond()` (identical to `_channel_allowed()`). Collapse the `get_permissions_for_channel()` mode branches (identical outputs). These are maintenance traps waiting to cause subtle bugs.

5. **Add integration tests for the API server.** The test suite covers config, state, sessions, fragments, and prompt assembly, but has zero coverage of `api_server.py` (the most complex module at 746 lines) and zero coverage of async code paths. Use `aiohttp.test_utils.TestClient` to test the API endpoints.

6. **Split `services/web/main.py` before it grows further.** At 857 lines with 8+ responsibilities, it is already approaching the size that motivated the v2 rewrite. The duplicate `CREATE TABLE IF NOT EXISTS notifications` schema on line 717 is a concrete correctness risk.
