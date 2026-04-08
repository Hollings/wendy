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
import shutil
import time
from pathlib import Path
from typing import Any

from . import sessions
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
from .paths import (
    STREAM_LOG_FILE,
    WENDY_BASE,
    beads_dir,
    channel_dir,
    current_session_file,
    ensure_channel_dirs,
    ensure_shared_dirs,
    session_dir,
)

_LOG = logging.getLogger(__name__)

TOOL_INSTRUCTIONS_TEMPLATE = """
---
REAL-TIME CHANNEL TOOLS (Channel ID: {channel_id})

1. SEND A MESSAGE (use the `msg` command):
   msg 'your message here'

   IMPORTANT: Always use single quotes (') around message content, NOT double quotes (").
   Double quotes cause the shell to eat $ signs: msg "$100" sends "00". Single quotes are safe: msg '$100' sends "$100".
   For messages containing single quotes, use a heredoc instead.

   Multiline messages (use heredoc with single-quoted delimiter):
   msg <<'EOF'
   Line one of your message.

   Line two with "quotes", $pecial characters, and $1,000 -- all fine.
   EOF

   With attachment (file can be anywhere under /data/wendy/ or /tmp/):
   msg -f /data/wendy/channels/{channel_name}/output.png 'check this out'

   Reply to a specific message (use sparingly - only when referencing a specific post for context):
   msg -r MESSAGE_ID 'great point'

   If the API returns an error about new messages, check them and incorporate into your reply. If you've already checked and want to send anyway:
   msg --force 'your message'

   The response includes a "new_messages" array with any messages that arrived while you were working. Check it -- if there are new messages, respond to them too before finishing.

2. ADD EMOJI REACTION (use the `react` command):
   react MESSAGE_ID EMOJI_NAME

   Examples:
   react 1484287499558977566 fire
   react 1484287499558977566 thumbsup
   react 1484287499558977566 100

   FORMAT: Use plain text emoji names -- NO colons, NO unicode characters, NO quotes needed.
   Correct: react 123 fire
   Wrong:   react 123 :fire:
   Wrong:   react 123 "\U0001f525"

   The MESSAGE_ID must be from the current channel (the one in your check_messages responses).

   Common names: thumbsup, fire, heart, laugh, eyes, thinking, 100, party, cool, rocket, skull, check, x, brain, sparkles, star, wave, clap, pray, salute, moai, nerd

3. SCHEDULE A SELF-WAKE (use the `wake` command):
   wake 15m "check on the build"
   wake 2h "follow up with delta about the PR"
   wake 14:30 "afternoon check-in"
   wake 2026-03-22T18:00 "evening review"

   Accepts a relative duration (30s, 15m, 2h) or an absolute UTC time (HH:MM or YYYY-MM-DDTHH:MM).
   All absolute times are UTC. Bare HH:MM wraps to tomorrow if already past.
   If a user asks to be woken at a local time, ask their timezone and convert to UTC yourself.
   You stay available for normal messages in the meantime.
   Only one wake per channel -- scheduling a new one replaces the previous. Min 10s, max 24h.

4. CHECK MESSAGES (use the `msgs` command):
   msgs                 # fetch new messages since last check
   msgs -n 10           # fetch last 10 messages
   msgs --all           # fetch all messages (ignores watermark)
   msgs --peek          # fetch without advancing the read watermark
   msgs --raw           # dump raw JSON (for debugging/parsing)

   This is what you MUST call at the start of every turn. Equivalent to the
   check_messages API but formatted for the terminal.

REPLIES AND REACTIONS:
- Replies aren't necessary for responding to the most recent message - only use when pointing at a specific post for context
- Reactions should be used sparingly for effect, not on every message
- message_id values come from msgs --raw output

ATTACHMENTS:
When users upload files (images, documents, code, etc.), the check_messages response includes an "attachments" array with file paths:
  {{"author": "someone", "content": "look at this", "attachments": ["/data/wendy/channels/{channel_name}/attachments/msg_123_0_photo.jpg"]}}
- You CANNOT see attachments without using the Read tool on the file path. The path is just a reference.
- If a message has an "attachments" array, you MUST call Read on each path to actually see the content.
- Do NOT describe or comment on files you haven't actually Read - you will hallucinate.
- Always check for the "attachments" field in message JSON when users seem to be sharing something.

PERSONAL FOLDER:
Your workspace for this channel is /data/wendy/channels/{channel_name}/
- Save notes, files, and project work here
- This persists between conversations

MESSAGE HISTORY DATABASE:
You have full read access to the message history at /data/wendy/shared/wendy.db. Use sqlite3 directly to search messages, check past conversations, or find old content.

Usage:
  sqlite3 /data/wendy/shared/wendy.db "SELECT * FROM message_history WHERE content LIKE '%keyword%' LIMIT 20"
  sqlite3 /data/wendy/shared/wendy.db .schema    # Show all tables

Key tables:
- message_history: Full raw messages (message_id, channel_id, guild_id, author_id, author_nickname, is_bot, content, timestamp, attachment_urls, reply_to_id)
  - message_id is the Discord message ID - you can make jump links: https://discord.com/channels/{{guild_id}}/{{channel_id}}/{{message_id}}
"""


class ClaudeCliError(Exception):
    """Base exception for Claude CLI errors."""

    def __init__(self, message: str, *, overloaded: bool = False) -> None:
        super().__init__(message)
        self.overloaded = overloaded


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
    ]
    if effort_args:
        cmd.extend(effort_args)

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
    channel_id: int,
    is_thread: bool = False,
    thread_name: str | None = None,
    journal_note: str = "",
    beads_note: str = "",
    was_compacted: bool = False,
) -> str:
    """Build the nudge prompt sent to Claude CLI via stdin."""
    if is_thread:
        base = (
            f'<you\'ve been forked into a Discord thread: "{thread_name}". '
            f"Your conversation history from the parent channel has been preserved. "
            f"You MUST run `msgs` before any other action. Do not assume what the messages contain.>"
        )
    else:
        base = (
            f"<new messages - you MUST run `msgs` "
            f"before any other action. Do not assume what the messages contain.>"
        )
    compacted_note = (
        f"<your session was auto-compacted since your last turn. "
        f"Use `msgs -n 20` THIS TIME to restore context. "
        f"After this, go back to plain `msgs` with no flags -- "
        f"do not use -n unless you have a specific reason.>"
    ) if was_compacted else ""
    extras = "\n".join(x for x in [journal_note, beads_note, compacted_note] if x)
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
        dest = dest_dir / script.name
        if not dest.exists() or dest.stat().st_mtime < script.stat().st_mtime:
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


def _build_cli_env(channel_name: str, channel_id: int, beads_enabled: bool) -> dict[str, str]:
    """Build the environment dict for the CLI subprocess.

    Strips sensitive variables, optionally sets BEADS_DIR, and points
    HOME at the wendy user when running with privilege separation.
    """
    cli_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
    if beads_enabled:
        cli_env["BEADS_DIR"] = str(beads_dir(channel_name))
    # Channel context for helper scripts (msg, react)
    cli_env["WENDY_CHANNEL_ID"] = str(channel_id)
    cli_env["WENDY_PROXY_PORT"] = str(PROXY_PORT)
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


async def _watch_session_for_overloaded(
    session_jsonl: Path,
    proc: asyncio.subprocess.Process,
    poll_interval: float = 3.0,
) -> None:
    """Poll the session JSONL for overloaded_error entries.

    The CLI swallows 529 overloaded errors internally and retries for
    ~4 minutes without emitting anything on stdout.  This watcher reads
    the tail of the session file every *poll_interval* seconds.  When it
    spots ``overloaded_error``, it kills the subprocess so the caller
    can retry with a different model.
    """
    # Record the file size at start so we only scan new bytes.
    try:
        initial_size = session_jsonl.stat().st_size
    except OSError:
        initial_size = 0

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
            with open(session_jsonl, "r", encoding="utf-8", errors="replace") as f:
                f.seek(initial_size)
                new_data = f.read()
        except OSError:
            continue
        if "overloaded_error" in new_data:
            _LOG.warning("Session JSONL contains overloaded_error, killing CLI")
            _kill_process(proc)
            return


async def _stream_cli_output(
    proc: asyncio.subprocess.Process,
    channel_id: int,
    idle_timeout: int,
    max_runtime: int,
    session_jsonl: Path | None = None,
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
                append_to_stream_log(event, channel_id)
                if event.get("type") == "result":
                    usage = event.get("usage", {})
                # Also check stdout in case the CLI does emit it here.
                if "overloaded_error" in decoded:
                    _LOG.warning("Detected overloaded_error in stream output")
                    overloaded_detected = True
                    _kill_process(proc)
                    break
            except json.JSONDecodeError:
                continue
    finally:
        if watcher_task is not None:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

    # If the watcher killed the process (EOF without overloaded in stream),
    # check whether the watcher detected the error.
    if not overloaded_detected and watcher_task is not None and watcher_task.done():
        # Watcher finished naturally (found overloaded and killed proc).
        overloaded_detected = True

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

    session_id, is_new_session, fork_mode = _resolve_session(
        channel_id, channel_config, session_cwd_folder, force_new_session,
    )

    effective_model = resolve_model(model_override or channel_config.get("model"))

    cmd = build_cli_command(
        cli_path, session_id, is_new_session, system_prompt,
        channel_config, effective_model, fork_mode=fork_mode,
        effort_args=effort_args, max_turns=max_turns,
    )

    from .prompt import get_beads_warning_for_nudge, get_journal_listing_for_nudge
    journal_note = get_journal_listing_for_nudge(channel_name)
    beads_note = get_beads_warning_for_nudge(channel_name) if beads_enabled else ""

    compacted_flag = channel_dir(channel_name) / ".compacted"
    was_compacted = compacted_flag.exists()
    if was_compacted:
        compacted_flag.unlink(missing_ok=True)
        from .fragments import reset_introductions
        reset_introductions(channel_name)

    nudge_prompt = nudge_override or build_nudge_prompt(
        channel_id, is_thread=is_thread, thread_name=thread_name,
        journal_note=journal_note, beads_note=beads_note,
        was_compacted=was_compacted,
    )

    # Ensure filesystem prerequisites.
    WENDY_BASE.mkdir(parents=True, exist_ok=True)
    setup_wendy_scripts()
    setup_channel_folder(channel_name, beads_enabled=beads_enabled)

    session_action = "starting new" if is_new_session else "resuming"
    _LOG.info("CLI: %s session %s for channel %d (model=%s)", session_action, session_id[:8], channel_id, effective_model)

    if beads_enabled:
        _write_current_session_file(channel_name, session_id)

    proc = None
    idle_timeout = CLAUDE_CLI_IDLE_TIMEOUT
    max_runtime = timeout_override if timeout_override is not None else CLAUDE_CLI_MAX_RUNTIME
    try:
        user_kwargs = {"user": CLI_SUBPROCESS_UID} if CLI_SUBPROCESS_UID else {}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=10 * 1024 * 1024,
            cwd=channel_dir(session_cwd_folder),
            env=_build_cli_env(channel_name, channel_id, beads_enabled),
            **user_kwargs,
        )

        proc.stdin.write(nudge_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        session_jsonl = session_dir(session_cwd_folder) / f"{session_id}.jsonl"
        events, usage = await _stream_cli_output(
            proc, channel_id, idle_timeout, max_runtime,
            session_jsonl=session_jsonl,
        )

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
        if proc.returncode != 0:
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
                    _write_current_session_file(channel_name, forked_id)

        if usage:
            sessions.update_stats(channel_id, usage)

        _LOG.info("CLI: completed, events_streamed=%d", len(events))

    except TimeoutError as exc:
        _kill_process(proc)
        raise ClaudeCliError(f"Timed out: {exc}") from None

    except asyncio.CancelledError:
        _kill_process(proc)
        raise
