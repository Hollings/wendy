"""Claude CLI subprocess manager.

Spawns and streams the ``claude`` CLI subprocess, manages session
resolution and forking, and writes stream/debug logs.  Wendy's
responses flow through the internal HTTP API, not stdout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import sessions, task_auth
from .config import (
    CLAUDE_CLI_IDLE_TIMEOUT,
    CLAUDE_CLI_MAX_RUNTIME,
    CLI_SUBPROCESS_UID,
    DEV_MODE,
    MAX_STREAM_LOG_LINES,
    PROXY_PORT,
    SENSITIVE_ENV_VARS,
    resolve_model,
)
from .conversation_clients import Client, clients
from .memory_reminders import reminder
from .paths import (
    STREAM_LOG_FILE,
    WENDY_BASE,
    beads_dir,
    channel_dir,
    current_session_file,
    ensure_channel_dirs,
    ensure_shared_dirs,
    journal_dir,
    session_dir,
)

_LOG = logging.getLogger(__name__)

TOOL_INSTRUCTIONS_TEMPLATE = """
---
CONVERSATION TOOLS (channel {channel_id})
Your workspace is /data/wendy/channels/{channel_name}/; files persist across turns.

msg 'text'                         # send to this conversation
msg -f /absolute/path 'caption'     # upload a deliverable
msg -r MESSAGE_ID 'text'            # reply to a specific earlier message
react MESSAGE_ID fire              # plain emoji name, no colons; use sparingly
msgs                               # read new messages when you choose to
msgs -n 20                         # retrieve recent context when needed
msgs --all                         # request history regardless of read watermark
msgs --raw                         # JSON with IDs and attachment paths
wake 15m 'check the build'          # schedule a self-wake; no polling needed

Use single quotes for shell message text so $ signs survive. For text containing
single quotes or multiple lines, use a heredoc with a quoted delimiter:
msg <<'EOF'
your message here
EOF
Read attachment paths with an appropriate file tool before describing their contents.
When you choose to catch up, continue msgs if it reports more unread messages.
If the inbox is empty, finish instead of polling. msgs is the only way to check
for new or unread messages: never poll the database, logs, or session files to
see whether something new arrived. Querying the database for older history is
fine (searching past messages, who said what when, counts and stats).

wenv status                        # inspect this conversation's preferences
wenv messages manual               # default: content only through explicit msgs
wenv messages auto                 # opt into delivery at turn/tool boundaries
wenv client warm                   # default: keep the idle client for reuse
wenv client cold                   # release after this turn; saved history remains
Preferences persist per conversation; threads are independent. In manual mode,
notices contain no message contents and you may ignore them and finish. Sending
never reads messages. If unread messages block a send, choose msgs to read them
or msg --force 'text' to send without reading. Auto delivery stops when switched
back to manual; already-delivered observations remain in your context.

Wake accepts durations or absolute UTC times; convert local times using the user's
timezone (ask if unknown). One wake per conversation, replaced by a new schedule;
minimum 10 seconds, maximum 24 hours.

Helpers authenticate automatically. Never print or save WENDY_API_TOKEN.
Detailed command examples: /app/config/docs/conversation_tools.md
Environment behavior: /app/config/docs/environment.md
---
"""


class ClaudeCliError(Exception):
    """Base exception for Claude CLI errors.

    ``overloaded`` marks a transient server-side API failure (529 overloaded
    or any other 5xx). The client uses it to decide whether the turn is
    worth retrying -- first with the in-turn model fallback ladder, then via
    the outage recovery timer once that ladder gives up.
    """

    def __init__(self, message: str, *, overloaded: bool = False) -> None:
        super().__init__(message)
        self.overloaded = overloaded


_TRANSIENT_API_FAILURE_RE = re.compile(
    r"overloaded"
    r"|api error:? *5\d\d"
    r"|internal server error"
    r"|service unavailable"
    r"|bad gateway"
    r"|gateway time-?out",
    re.IGNORECASE,
)


def is_transient_api_failure(detail: str | None) -> bool:
    """True if a CLI failure message describes a retryable server-side API error.

    Live example: a turn died on ``API Error: 500 Internal server error``
    minutes before the 529s started; it was treated as a hard failure and the
    channel went silent until the next human message.
    """
    return bool(detail) and bool(_TRANSIENT_API_FAILURE_RE.search(detail))


def find_cli_path() -> str:
    """Find the claude CLI executable."""
    cli_path = os.getenv("CLAUDE_CLI_PATH")
    if cli_path and Path(cli_path).exists():
        return cli_path

    candidates = [
        str(Path.home() / ".local" / "bin" / "claude"),
        str(Path.home() / ".claude" / "local" / "claude"),
        shutil.which("claude"),
    ]

    for path in candidates:
        if path and Path(path).exists():
            return path

    raise ClaudeCliError("Claude CLI not found. Install it or set CLAUDE_CLI_PATH env var.")


def get_permissions_for_channel(channel_config: dict) -> tuple[str, str]:
    """Return (allowedTools, disallowedTools) strings for the CLI invocation.

    Permissions are channel-scoped: the bot can only write inside its own
    channel directory and the shared fragments directory.  In dev mode the
    write restrictions are relaxed.
    """
    channel_name = channel_config.get("_folder", channel_config.get("name", "default"))

    allowed = (
        f"Read,WebSearch,WebFetch,Bash,"
        f"Edit(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/channels/{channel_name}/**),"
        f"Edit(//data/wendy/claude_fragments/people/**),Write(//data/wendy/claude_fragments/people/**),"
        f"Write(//data/wendy/tmp/**),Write(//tmp/**)"
    )
    disallowed = "Edit(//app/**),Write(//app/**),Skill,TodoWrite,TodoRead"

    from .memory_export import enabled as memory_enabled
    if memory_enabled():
        allowed += ',mcp__memory__research_memory,mcp__memory__open_memory_evidence'

    if DEV_MODE:
        allowed += ",Edit(//data/wendy/dev-repo/**),Write(//data/wendy/dev-repo/**)"
        disallowed = ""

    return allowed, disallowed


def build_cli_command(
    cli_path: str,
    session_id: str,
    is_new_session: bool,
    system_prompt: str,
    channel_config: dict,
    model: str,
    fork_mode: bool = False,
    effort_args: list[str] | None = None,
    max_turns: int | None = None,
) -> list[str]:
    """Build the full ``claude`` CLI argv list.

    Handles session-id vs resume vs fork flags, model selection,
    system prompt injection, and tool permission flags.
    """
    cmd = [
        cli_path,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--strict-mcp-config",
        # Without this, thinking blocks arrive with an empty `thinking` field on
        # 5-generation models and the brain feed has no thoughts to show. The
        # CLI only defaults to "summarized" in interactive mode; headless (-p)
        # leaves it at the API default of "omitted". Costs nothing extra --
        # the model thinks and is billed the same either way, this only
        # controls whether a readable summary comes back with it.
        "--thinking-display", "summarized",
    ]
    if effort_args:
        cmd.extend(effort_args)

    from .memory_export import enabled as memory_enabled
    if memory_enabled():
        cmd.extend(['--mcp-config', json.dumps({'mcpServers': {'memory': {
            # The controller runs in a writable workspace containing helpers
            # such as secrets.py. Keep that directory off Python's import path
            # so those files cannot shadow the MCP server's dependencies.
            'command': sys.executable, 'args': ['-P', '-m', 'wendy.memory_mcp'],
            'env': {'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
        }}})])

    if fork_mode:
        cmd.extend(["--resume", session_id, "--fork-session"])
    elif is_new_session:
        cmd.extend(["--session-id", session_id])
    else:
        cmd.extend(["--resume", session_id])

    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])

    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    allowed_tools, disallowed_tools = get_permissions_for_channel(channel_config)
    cmd.extend(["--allowedTools", allowed_tools, "--disallowedTools", disallowed_tools])

    return cmd


def build_nudge_prompt(
    is_thread: bool = False,
    thread_name: str | None = None,
    journal_note: str = "",
    beads_note: str = "",
    roster_note: str = "",
    was_compacted: bool = False,
) -> str:
    """Build the nudge prompt sent to Claude CLI via stdin."""
    if is_thread:
        base = (
            f'<you are in a Discord thread: "{thread_name}". '
            f"Your conversation history from the parent channel has been preserved. "
            f"Use msgs if you choose to read pending messages in manual mode. Do not assume their contents.>"
        )
    else:
        base = (
            "<new messages - check your inbox when ready. In manual mode, use `msgs` "
            "to read them; you may choose to leave them unread. Do not assume their contents. "
            "If `msgs` reports no new messages, do NOT run it again to poll or wait "
            "for a reply -- end your turn silently. You are woken automatically "
            "the moment a new message arrives.>"
        )
    compacted_note = (
        "<your session was auto-compacted since your last turn. "
        "If you choose to restore recent message context, use `msgs -n 20`. "
        "Otherwise you may leave messages unread. Use plain `msgs` for new messages.>"
    ) if was_compacted else ""
    extras = "\n".join(x for x in [roster_note, journal_note, beads_note, compacted_note] if x)
    return base + ("\n" + extras if extras else "")


def setup_channel_folder(channel_name: str, beads_enabled: bool = False) -> None:
    """Create channel workspace and sync Claude Code settings from the app config."""
    ensure_channel_dirs(channel_name, beads_enabled=beads_enabled)
    chan_dir = channel_dir(channel_name)

    claude_settings_src = Path("/app/config/claude_settings.json")
    claude_dir = chan_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_dest = claude_dir / "settings.json"
    if claude_settings_src.exists():
        if not settings_dest.exists() or settings_dest.stat().st_mtime < claude_settings_src.stat().st_mtime:
            shutil.copy2(claude_settings_src, settings_dest)


def _sync_scripts(src_dir: Path, dest_dir: Path, pattern: str, *, make_executable: bool = False) -> None:
    """Copy scripts from *src_dir* to *dest_dir* when the source is newer."""
    for script in src_dir.glob(pattern):
        if not script.is_file():  # skip stray dirs like bin/__pycache__/
            continue
        dest = dest_dir / script.name
        if script.name == 'bd' or not dest.exists() or dest.stat().st_mtime < script.stat().st_mtime:
            # npm exposes commands as symlinks. Replace the public link itself,
            # never follow it and overwrite the controller's real BD backend.
            if dest.is_symlink():
                dest.unlink()
            shutil.copy2(script, dest)
            if make_executable:
                dest.chmod(0o755)


def setup_wendy_scripts() -> None:
    """Sync helper scripts to the data volume and ensure shared dirs exist."""
    scripts_src = Path("/app/scripts")
    if scripts_src.exists():
        _sync_scripts(scripts_src, WENDY_BASE, "*.sh", make_executable=True)
        _sync_scripts(scripts_src, WENDY_BASE, "*.py")

    # Install CLI helper scripts (msg, react) to PATH
    bin_src = Path("/app/bin")
    if bin_src.exists():
        _sync_scripts(bin_src, Path("/usr/local/bin"), "*", make_executable=True)

    ensure_shared_dirs()

    secrets_dir = WENDY_BASE / "secrets"
    secrets_dir.mkdir(exist_ok=True, mode=0o700)


def append_to_stream_log(event: dict, channel_id: int | None) -> None:
    """Append a single event to the rolling stream log file."""
    try:
        STREAM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        enriched = {
            "ts": int(time.time() * 1000),
            "channel_id": str(channel_id) if channel_id else None,
            "event": event,
        }
        with open(STREAM_LOG_FILE, "a") as f:
            f.write(json.dumps(enriched) + "\n")
    except Exception as e:
        _LOG.error("Failed to append to stream log: %s", e)


def trim_stream_log() -> None:
    """Trim stream log to MAX_STREAM_LOG_LINES."""
    try:
        if not STREAM_LOG_FILE.exists():
            return
        with open(STREAM_LOG_FILE) as f:
            lines = f.readlines()
        if len(lines) > MAX_STREAM_LOG_LINES:
            with open(STREAM_LOG_FILE, "w") as f:
                f.writelines(lines[-MAX_STREAM_LOG_LINES:])
    except Exception as e:
        _LOG.error("Failed to trim stream log: %s", e)


def save_debug_log(events: list[dict], channel_id: int | None) -> None:
    """Save CLI events to debug log file (keeps last 20)."""
    try:
        debug_dir = Path("/data/wendy/debug_logs")
        debug_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time() * 1000)
        channel_str = str(channel_id) if channel_id else "unknown"
        log_path = debug_dir / f"{channel_str}_{timestamp}.json"

        log_path.write_text(json.dumps({
            "timestamp": timestamp,
            "channel_id": channel_id,
            "events": events,
        }, indent=2))

        logs = sorted(debug_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old_log in logs[:-20]:
            old_log.unlink()
    except Exception as e:
        _LOG.error("Failed to save debug log: %s", e)


def get_recent_cli_error() -> str | None:
    """Parse the most recent Claude CLI debug log for a human-readable error.

    Checks for known patterns (OAuth expiry, authentication errors) first,
    then falls back to the last ``[ERROR]`` line in the file.  Returns
    ``None`` if no debug files exist or no error is found.
    """
    debug_dir = Path.home() / ".claude" / "debug"
    if not debug_dir.exists():
        return None
    try:
        debug_files = sorted(debug_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not debug_files:
            return None

        content = debug_files[0].read_text(errors="replace")

        if "OAuth token has expired" in content:
            return "OAuth token has expired"
        if "authentication_error" in content:
            import re
            match = re.search(r'"message":\s*"([^"]+)"', content)
            return match.group(1) if match else "authentication error"

        for line in reversed(content.strip().split("\n")[-20:]):
            if "[ERROR]" in line:
                if "Error:" in line:
                    return line.split("Error:", 1)[-1].strip()[:200]
                return line.split("[ERROR]", 1)[-1].strip()[:200]

    except Exception as e:
        _LOG.warning("Failed to read CLI debug files: %s", e)
    return None


def extract_forked_session_id(events: list[dict], session_cwd_folder: str) -> str | None:
    """Extract the forked session ID from stream-json events.

    Checks (in priority order): ``result`` events, ``system`` events,
    then falls back to the ``sessions-index.json`` file on disk.
    """
    for event in reversed(events):
        if event.get("type") == "result" and event.get("session_id"):
            return event["session_id"]
    for event in events:
        if event.get("type") == "system" and event.get("session_id"):
            return event["session_id"]

    try:
        index_path = session_dir(session_cwd_folder) / "sessions-index.json"
        if index_path.exists():
            index = json.loads(index_path.read_text())
            entries = index.get("entries", [])
            if entries:
                entries.sort(key=lambda e: e.get("modified", ""), reverse=True)
                return entries[0].get("sessionId")
    except Exception as e:
        _LOG.warning("Failed to read sessions-index.json: %s", e)

    return None


def _write_current_session_file(channel_name: str, session_id: str) -> None:
    """Atomically write *session_id* to the channel's current-session file.

    Used by the beads orchestrator to know which session to fork from.
    Writes to a temp file first, then renames for atomicity.
    """
    cs_file = current_session_file(channel_name)
    try:
        temp_file = cs_file.with_suffix(".tmp")
        temp_file.write_text(session_id)
        temp_file.replace(cs_file)
    except Exception as e:
        _LOG.warning("Failed to write current session file: %s", e)


def _resolve_session(
    channel_id: int,
    channel_config: dict,
    session_cwd_folder: str,
    force_new_session: bool,
) -> tuple[str, bool, bool]:
    """Determine the session ID and whether to create/resume/fork.

    Returns:
        (session_id, is_new_session, fork_mode)
    """
    is_thread = channel_config.get("_is_thread", False)
    parent_folder = channel_config.get("_parent_folder")

    session_info = sessions.get_session(channel_id)

    channel_changed = (
        session_info is not None
        and session_info.folder != session_cwd_folder
    )
    if channel_changed:
        _LOG.warning(
            "Channel folder changed for %d: %s -> %s",
            channel_id, session_info.folder, session_cwd_folder,
        )

    is_new_session = session_info is None or force_new_session or channel_changed

    # If session exists in DB but JSONL is missing on disk (e.g. after !clear),
    # treat as new so we use --session-id instead of --resume.
    if not is_new_session and session_info:
        sess_file = session_dir(session_cwd_folder) / f"{session_info.session_id}.jsonl"
        if not sess_file.exists():
            _LOG.info("Session %s has no JSONL on disk, treating as new", session_info.session_id[:8])
            is_new_session = True

    # For new thread sessions, try to fork from parent.
    fork_mode = False
    session_id = ""
    if is_new_session and is_thread and parent_folder:
        parent_channel_id = int(channel_config.get("_parent_channel_id", 0))
        parent_session = sessions.get_session(parent_channel_id)
        if parent_session:
            parent_sess_file = session_dir(session_cwd_folder) / f"{parent_session.session_id}.jsonl"
            if parent_sess_file.exists():
                session_id = parent_session.session_id
                fork_mode = True
                _LOG.info(
                    "Thread fork: --resume %s --fork-session from parent %s",
                    session_id[:8], parent_folder,
                )

    if is_new_session and not fork_mode:
        session_id = sessions.create_session(channel_id, session_cwd_folder)
    elif not is_new_session:
        session_id = session_info.session_id

    return session_id, is_new_session, fork_mode


def _build_cli_env(
    channel_name: str,
    channel_id: int,
    beads_enabled: bool,
    enrichment: bool = False,
    beads_folder: str | None = None,
) -> dict[str, str]:
    """Build the environment dict for the CLI subprocess.

    Strips sensitive variables, optionally sets BEADS_DIR, and points
    HOME at the wendy user when running with privilege separation.
    ``beads_folder`` overrides which channel's .beads BEADS_DIR points at
    (threads use their parent channel's queue).
    """
    cli_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
    if beads_enabled:
        cli_env["BEADS_DIR"] = str(beads_dir(beads_folder or channel_name))
    # Channel context for helper scripts (msg, react)
    cli_env["WENDY_CHANNEL_ID"] = str(channel_id)
    cli_env["WENDY_PROXY_PORT"] = str(PROXY_PORT)
    if enrichment:
        # Lets hooks skip checks that conflict with lunch mode (the unread
        # stop hook would demand `msgs`, which the API 403s during lunch).
        cli_env["WENDY_ENRICHMENT"] = "1"
    # Pass auth and sync tokens explicitly so the CLI can authenticate even though
    # they're stripped from the general env (to keep them out of `env` output).
    if oauth_token := os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        cli_env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    if sync_key := os.environ.get("CLAUDE_SYNC_KEY"):
        cli_env["CLAUDE_SYNC_KEY"] = sync_key
    # Point HOME at the wendy user's home directory for CLI isolation
    if CLI_SUBPROCESS_UID is not None:
        cli_env["HOME"] = "/home/wendy"
    return cli_env


def _is_session_resume_error(cmd: list[str], error_text: str) -> bool:
    """Return True if the CLI failure looks like a stale/missing session."""
    if "--resume" not in cmd:
        return False
    lower = error_text.lower()
    return "session" in lower or "no conversation found" in lower


def _jsonl_line_is_overloaded(line: str) -> bool:
    """Return True if a session-JSONL line is an actual API overloaded error.

    Substring matching alone is not enough: conversation content can contain
    the literal string ``overloaded_error`` (a pasted error log, Wendy reading
    her own source), which used to kill healthy sessions. Only trust entries
    the CLI marks as API errors, or non-conversation record types.
    """
    if "overloaded_error" not in line:
        return False
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return False
    if entry.get("isApiErrorMessage"):
        return True
    return entry.get("type") not in ("user", "assistant", "summary")


def _stream_event_is_overloaded(event: dict) -> bool:
    """Return True if a stream-json event is an actual overloaded API error
    (not conversation content quoting the string)."""
    if event.get("isApiErrorMessage"):
        return True
    etype = event.get("type")
    if etype == "result":
        return bool(event.get("is_error"))
    return etype not in ("user", "assistant")


async def _watch_session_for_overloaded(
    session_jsonl: Path,
    proc: asyncio.subprocess.Process,
    poll_interval: float = 3.0,
) -> bool:
    """Poll the session JSONL for overloaded_error entries.

    The CLI swallows 529 overloaded errors internally and retries for
    ~4 minutes without emitting anything on stdout.  This watcher reads
    the tail of the session file every *poll_interval* seconds.  When it
    spots an overloaded API error entry, it kills the subprocess so the
    caller can retry with a different model.

    Returns True only when an overloaded error was detected (and the
    process killed); False when the loop exits because the CLI ended on
    its own.  The caller must use this return value, not task-completion
    state, to decide whether an overload happened.
    """
    # Record the file size at start so we only scan new bytes.
    try:
        initial_size = session_jsonl.stat().st_size
    except OSError:
        initial_size = 0

    partial = ""
    while proc.returncode is None:
        await asyncio.sleep(poll_interval)
        try:
            current_size = session_jsonl.stat().st_size
        except OSError:
            continue
        if current_size <= initial_size:
            continue
        # Read only the new tail.
        try:
            with open(session_jsonl, encoding="utf-8", errors="replace") as f:
                f.seek(initial_size)
                new_data = f.read()
        except OSError:
            continue
        initial_size = current_size
        # Parse complete lines only; carry any trailing partial line over to
        # the next poll so a mid-write read can't corrupt the JSON parse.
        lines = (partial + new_data).split("\n")
        partial = lines.pop()
        if any(_jsonl_line_is_overloaded(line) for line in lines):
            _LOG.warning("Session JSONL contains overloaded API error, killing CLI")
            _kill_process(proc)
            return True
    return False


async def _stream_cli_output(
    proc: asyncio.subprocess.Process,
    channel_id: int,
    idle_timeout: int,
    max_runtime: int,
    session_jsonl: Path | None = None,
    controller_token: str | None = None,
    stop_on_result: bool = False,
) -> tuple[list[dict], dict[str, Any]]:
    """Read stream-json events from the CLI subprocess stdout.

    Uses an **idle timeout** rather than a wall-clock cap: the timer resets
    every time a line of output arrives.  A separate *max_runtime* acts as
    an absolute safety net for runaway sessions.

    If *session_jsonl* is provided, a background watcher polls the file
    for ``overloaded_error`` entries and kills the process immediately
    so we don't wait for the CLI's ~4 min internal retry loop.

    Returns:
        (events, usage) where *usage* comes from the ``result`` event.
    """
    # Start the overloaded watcher if we have a session file path.
    watcher_task: asyncio.Task | None = None
    if session_jsonl is not None:
        watcher_task = asyncio.create_task(
            _watch_session_for_overloaded(session_jsonl, proc)
        )

    events: list[dict] = []
    usage: dict[str, Any] = {}
    start = time.monotonic()
    overloaded_detected = False

    try:
        while True:
            elapsed = time.monotonic() - start
            remaining = max_runtime - elapsed
            if remaining <= 0:
                _LOG.error("CLI hit max runtime of %ds", max_runtime)
                raise TimeoutError(f"hit max runtime ({max_runtime}s)")

            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=min(idle_timeout, remaining),
                )
            except TimeoutError:
                elapsed = time.monotonic() - start
                if elapsed >= max_runtime - 1:
                    msg = f"hit max runtime ({max_runtime}s)"
                else:
                    msg = f"idle for {idle_timeout}s (total runtime {elapsed:.0f}s)"
                _LOG.error("CLI %s", msg)
                raise TimeoutError(msg) from None

            if not raw:  # EOF -- process closed stdout
                break

            decoded = raw.decode("utf-8").strip()
            if not decoded:
                continue
            try:
                event = json.loads(decoded)
                events.append(event)
                if len(events) == 1:
                    _LOG.info('CLI first event: channel=%d elapsed_ms=%.1f', channel_id,
                              (time.monotonic() - start) * 1000)
                if event.get('type') == 'system' and event.get('subtype') == 'init' and event.get('model'):
                    # --model may be a bare alias (e.g. "opus"); record what it resolved to.
                    _LOG.info('CLI init: channel=%d model=%s', channel_id, event['model'])
                if event.get('type') == 'system' and event.get('session_id') and controller_token:
                    scope = task_auth.lookup(controller_token)
                    if scope:
                        scope['session_id'] = event['session_id']
                append_to_stream_log(event, channel_id)
                if event.get("type") == "result":
                    usage = event.get("usage", {})
                    if stop_on_result:
                        break
                # Also check stdout in case the CLI does emit it here.
                if "overloaded_error" in decoded and _stream_event_is_overloaded(event):
                    _LOG.warning("Detected overloaded_error in stream output")
                    overloaded_detected = True
                    _kill_process(proc)
                    break
            except json.JSONDecodeError:
                continue
    finally:
        if watcher_task is not None:
            watcher_task.cancel()
            # The watcher's return value is the ONLY reliable overload signal:
            # it also finishes uncancelled (returning False) when the CLI
            # exits on its own and the watcher's poll notices before we tear
            # it down -- inferring from done()/cancelled() state misreported
            # those successful turns as overloaded.
            try:
                if await watcher_task:
                    overloaded_detected = True
            except asyncio.CancelledError:
                pass
            except Exception:
                # A watcher crash must not fail an otherwise-good turn.
                _LOG.warning("Overloaded watcher crashed", exc_info=True)

    if overloaded_detected:
        raise ClaudeCliError("API returned overloaded_error", overloaded=True)

    return events, usage


def _kill_process(proc: asyncio.subprocess.Process | None) -> None:
    """Kill *proc* if it is still running, swallowing errors."""
    if proc is None:
        return
    if proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass


async def run_cli(
    channel_id: int,
    channel_config: dict,
    system_prompt: str,
    model_override: str | None = None,
    force_new_session: bool = False,
    effort_args: list[str] | None = None,
    nudge_override: str | None = None,
    timeout_override: int | None = None,
    max_turns: int | None = None,
    enrichment: bool = False,
) -> None:
    """Spawn the Claude CLI subprocess and stream its output.

    This is the main entry point for running the Claude CLI.  Wendy's
    user-visible responses are sent through the internal HTTP API; stdout
    is consumed only for session tracking and debug logging.

    On a session-resume failure the call retries once with a fresh session.
    """
    cli_path = find_cli_path()
    channel_name = channel_config.get("_folder", channel_config.get("name", "default"))
    beads_enabled = channel_config.get("beads_enabled", False)

    is_thread = channel_config.get("_is_thread", False)
    parent_folder = channel_config.get("_parent_folder")
    thread_name = channel_config.get("_thread_name")

    # For threads, sessions live in the parent's project directory.
    session_cwd_folder = parent_folder if (is_thread and parent_folder) else channel_name

    # Beads are per-CHANNEL, never per-thread: the task runner only polls
    # configured channels, so a thread must point BEADS_DIR (and the fork
    # pointer) at its parent's queue or its tasks would sit in an
    # uninitialized .beads/ nobody ever reads.
    beads_folder = session_cwd_folder

    session_id, is_new_session, fork_mode = _resolve_session(
        channel_id, channel_config, session_cwd_folder, force_new_session,
    )

    effective_model = resolve_model(
        model_override or channel_config.get("model"),
        allow_env_override=model_override is None,
    )

    cmd = build_cli_command(
        cli_path, session_id, is_new_session, system_prompt,
        channel_config, effective_model, fork_mode=fork_mode,
        effort_args=effort_args, max_turns=max_turns,
    )

    from .prompt import (
        get_beads_warning_for_nudge,
        get_context_roster_for_nudge,
        get_journal_listing_for_nudge,
    )
    journal_note = get_journal_listing_for_nudge(channel_name)
    roster_note = get_context_roster_for_nudge(channel_id)
    # bd is an external subprocess -- keep it off the event loop.
    beads_note = await asyncio.to_thread(get_beads_warning_for_nudge, beads_folder) if beads_enabled else ""

    # The compaction flag is written by pre_compact.sh into the CLI's cwd
    # (the parent folder for threads) and is session-scoped -- a bare
    # .compacted couldn't tell a thread's compaction from its parent's.
    flag_dir = channel_dir(session_cwd_folder)
    compacted_flag = flag_dir / f".compacted_{session_id}"
    legacy_flag = flag_dir / ".compacted"
    was_compacted = compacted_flag.exists() or legacy_flag.exists()
    if was_compacted:
        compacted_flag.unlink(missing_ok=True)
        legacy_flag.unlink(missing_ok=True)

    memory_note = await asyncio.to_thread(reminder, channel_id, journal_dir(channel_name)) if nudge_override is None else ''
    journal_note = '\n'.join(note for note in (journal_note, memory_note) if note)
    nudge_prompt = nudge_override or build_nudge_prompt(
        is_thread=is_thread, thread_name=thread_name,
        journal_note=journal_note, beads_note=beads_note,
        roster_note=roster_note, was_compacted=was_compacted,
    )

    # Ensure filesystem prerequisites.
    WENDY_BASE.mkdir(parents=True, exist_ok=True)
    setup_wendy_scripts()
    # Threads never get their own .beads dir -- they use the parent's queue.
    setup_channel_folder(channel_name, beads_enabled=beads_enabled and not is_thread)

    session_action = "starting new" if is_new_session else "resuming"
    _LOG.info("CLI: %s session %s for channel %d (model=%s)", session_action, session_id[:8], channel_id, effective_model)

    if beads_enabled:
        # The fork pointer tracks the most recent session in the channel's
        # workspace (thread sessions included -- they share the parent cwd, so
        # their JSONLs are forkable from the same project dir). A bead created
        # in a thread forks the thread's own context.
        _write_current_session_file(beads_folder, session_id)

    proc = None
    client = None
    healthy = False
    use_persistent = not enrichment and os.getenv('WENDY_PERSISTENT_CLIENTS', '1') != '0'
    idle_timeout = CLAUDE_CLI_IDLE_TIMEOUT
    max_runtime = timeout_override if timeout_override is not None else CLAUDE_CLI_MAX_RUNTIME
    api_token = ''
    if not use_persistent:
        # Enrichment may append to this saved session in a separate process.
        # Discard the older in-memory conversation before that happens.
        await clients.close_channel(channel_id)
    try:
        user_kwargs = {"user": CLI_SUBPROCESS_UID} if CLI_SUBPROCESS_UID else {}
        env = _build_cli_env(channel_name, channel_id, beads_enabled, enrichment=enrichment, beads_folder=beads_folder)

        async def launch():
            token = task_auth.issue(role='controller', channel_id=channel_id,
                                    queue=beads_folder, session_id=session_id, active=True)
            try:
                child = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, limit=10 * 1024 * 1024,
                    cwd=channel_dir(session_cwd_folder), env={**env, 'WENDY_API_TOKEN': token, 'WENDY_MEMORY_JOURNAL': str(journal_dir(channel_name))},
                    start_new_session=os.name == 'posix', **user_kwargs)
            except BaseException:
                task_auth.revoke(token)
                raise
            return Client(child, token, task_auth.lookup(token), signature, session_id)

        signature_parts = []
        skip = False
        for part in cmd:
            if skip:
                skip = False
            elif part in ('--resume', '--session-id'):
                skip = True
            elif part != '--fork-session':
                signature_parts.append(part)
        # Configuration changes replace the process while preserving the session.
        settings = channel_dir(channel_name) / '.claude/settings.json'
        signature = (*signature_parts, settings.stat().st_mtime_ns if settings.exists() else 0)
        launch_start = time.monotonic()
        if use_persistent:
            cmd.extend(['--input-format', 'stream-json'])
            client, reused = await clients.acquire(channel_id, session_id, signature, launch)
            proc, api_token = client.process, client.token
            envelope = {'type': 'user', 'message': {'role': 'user', 'content': nudge_prompt},
                        'session_id': client.session_id, 'parent_tool_use_id': None}
            proc.stdin.write((json.dumps(envelope) + '\n').encode())
            await proc.stdin.drain()
            _LOG.info('CLI transport ready: warm=%s setup_ms=%.1f', reused, (time.monotonic() - launch_start) * 1000)
        else:
            ephemeral = await launch()
            proc, api_token = ephemeral.process, ephemeral.token
            proc.stdin.write(nudge_prompt.encode('utf-8'))
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

        session_jsonl = session_dir(session_cwd_folder) / f"{session_id}.jsonl"
        events, usage = await _stream_cli_output(
            proc, channel_id, idle_timeout, max_runtime,
            session_jsonl=session_jsonl,
            controller_token=api_token,
            stop_on_result=use_persistent,
        )

        if use_persistent:
            result = next((event for event in reversed(events) if event.get('type') == 'result'), None)
            if result is None:
                raise ClaudeCliError('CLI closed before completing the turn; saved session retained')
            if result.get('is_error') or result.get('subtype', 'success') != 'success':
                detail = str(result.get('result') or result.get('errors') or result.get('subtype'))
                raise ClaudeCliError(f'CLI turn failed: {detail}', overloaded=is_transient_api_failure(detail))
        else:
            await proc.wait()

        if proc.returncode == 0 and not events:
            _LOG.warning("CLI exited 0 but produced no events")

        # Check for overloaded error in result events (CLI exits 0 but
        # the result contains the API error).
        if proc.returncode == 0:
            for ev in events:
                if (
                    ev.get("type") == "result"
                    and ev.get("is_error")
                    and "overloaded_error" in str(ev.get("result", ""))
                ):
                    _LOG.warning("CLI returned overloaded_error result for channel %d", channel_id)
                    raise ClaudeCliError(
                        "CLI succeeded but API returned overloaded_error",
                        overloaded=True,
                    )

        # Handle CLI failure.
        if not use_persistent and proc.returncode != 0:
            error_detail = get_recent_cli_error() or "unknown error"
            _LOG.error("CLI failed (code %d): %s", proc.returncode, error_detail)
            if _is_session_resume_error(cmd, error_detail) and not force_new_session:
                _LOG.warning("Session resume failed, retrying with fresh session for channel %d", channel_id)
                return await run_cli(
                    channel_id, channel_config, system_prompt,
                    model_override=model_override, force_new_session=True,
                    effort_args=effort_args,
                    nudge_override=nudge_override,
                    timeout_override=timeout_override,
                    max_turns=max_turns,
                    enrichment=enrichment,
                )
            is_overloaded = "overloaded" in error_detail.lower()
            raise ClaudeCliError(
                f"CLI failed (code {proc.returncode}): {error_detail}",
                overloaded=is_overloaded,
            )

        save_debug_log(events, channel_id)
        trim_stream_log()

        # Register the forked session for thread channels.
        if fork_mode:
            forked_id = extract_forked_session_id(events, session_cwd_folder)
            if forked_id:
                sessions.create_session(channel_id, session_cwd_folder, session_id=forked_id)
                _LOG.info("Thread fork complete: parent=%s -> forked=%s", session_id[:8], forked_id[:8])
                if beads_enabled:
                    _write_current_session_file(beads_folder, forked_id)

        if usage:
            sessions.update_stats(channel_id, usage)

        _LOG.info("CLI: completed, events_streamed=%d", len(events))
        healthy = True

    except TimeoutError as exc:
        raise ClaudeCliError(f"Timed out: {exc}") from None

    except asyncio.CancelledError:
        raise
    finally:
        if client:
            from .environment import preferences
            from .state import state
            await clients.release(channel_id, client, healthy=healthy,
                                  keep_warm=preferences(state, channel_id)['client'] == 'warm')
        else:
            task_auth.revoke(api_token)
            if proc:
                from .worker_runtime import stop_process
                await stop_process(proc)
