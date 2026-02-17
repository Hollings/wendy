"""Claude CLI subprocess manager.

Spawns and streams the `claude` CLI subprocess. Nothing else.
~300 lines, subprocess management only.
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
    CLAUDE_CLI_TIMEOUT,
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

# Tool instructions template - {channel_id}, {channel_name}, and {proxy_port} are substituted
# Lives here in Phase 1, moves to prompt.py in Phase 2
TOOL_INSTRUCTIONS_TEMPLATE = """
---
REAL-TIME CHANNEL TOOLS (Channel ID: {channel_id})

CRITICAL: You are running in HEADLESS MODE. Your final output is NOT sent to Discord.
You MUST use the send_message API to respond - this is the ONLY way users will see your messages!

RESPONSE EXPECTATIONS:
- You should ALMOST ALWAYS respond. Users expect you to participate in conversation.
- If you don't call send_message, users see NOTHING - it looks like you ignored them.
- Only skip responding if users EXPLICITLY say they don't want your input (e.g., "wendy stop", "shut up", "go away").
- In ambiguous situations, neutral chats, or when unsure: RESPOND. Err on the side of engaging.
- Even a brief acknowledgment ("gotcha!", "nice", "haha") is better than silence.

1. SEND A MESSAGE (REQUIRED to respond):
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "your message here"}}'

   With attachment (file can be anywhere under /data/wendy/ or /tmp/):
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "check this out", "attachment": "/data/wendy/channels/{channel_name}/output.png"}}'

   Reply to a specific message (use sparingly - only when referencing a specific post for context):
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "great point", "reply_to": MESSAGE_ID}}'

   This is the ONLY way to send messages to users. Your final output goes nowhere.

2. CHECK FOR NEW MESSAGES (optional, use before responding):
   curl -s http://localhost:{proxy_port}/api/check_messages/{channel_id}

   Shows the last 10 messages to see if anyone sent new messages while you were thinking.
   Note: Always use -s flag with curl for cleaner output.

3. ADD EMOJI REACTION (use sparingly for effect):
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "actions": [{{"type": "add_reaction", "message_id": MESSAGE_ID, "emoji": "thumbsup"}}]}}'

   Batch actions (send message + react in one call):
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "actions": [{{"type": "send_message", "content": "nice!", "reply_to": MSG_ID}}, {{"type": "add_reaction", "message_id": MSG_ID, "emoji": "fire"}}]}}'

4. SEARCH CUSTOM EMOJIS (for custom server emojis):
   curl -s "http://localhost:{proxy_port}/api/emojis?search=keyword"

WORKFLOW:
1. Read/process the user's request
2. Do any work needed (read files, search, etc.)
3. ALWAYS call the send_message API to reply (unless explicitly told not to)
4. You can send multiple messages if needed

REPLIES AND REACTIONS:
- Replies aren't necessary for responding to the most recent message - only use when pointing at a specific post for context
- Reactions should be used sparingly for effect, not on every message
- You MUST use raw Unicode emoji characters, NOT text names. Examples: "emoji": "\\U0001f44d" (not "thumbsup"), "emoji": "\\U0001f525" (not "fire"), "emoji": "\\u2764\\ufe0f" (not "heart")
- Common emojis: \\U0001f44d \\U0001f525 \\u2764\\ufe0f \\U0001f602 \\U0001f440 \\U0001f914 \\U0001f4af \\U0001f389 \\U0001f60e \\U0001f680
- Custom server emojis need lookup via /api/emojis, use the "usage" field value (format: <:name:id>)
- message_id values come from check_messages responses

LONG TASKS:
Before doing something that might take a while (writing code, researching, reading multiple files, etc.), send a quick message to let users know. Otherwise they might think you froze or crashed. Send a quick "gimme a sec..." then do the work, then send your actual response.

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

SELF-CUSTOMIZATION:
Your channel instructions are assembled from fragment files in /data/wendy/claude_fragments/.
Files matching common_*.md and {channel_id}_*.md are loaded for this channel, sorted by the 2-digit order number.
You can edit any fragment file to customize behavior. Changes take effect on the next message.
To see available fragments: ls /data/wendy/claude_fragments/

MESSAGE HISTORY DATABASE:
You have full read access to the message history at /data/wendy/shared/wendy.db. Use query_db.py to search messages, check past conversations, or find old content.

Usage:
  python3 /app/scripts/query_db.py "SELECT * FROM message_history WHERE content LIKE '%keyword%' LIMIT 20"
  python3 /app/scripts/query_db.py --schema    # Show all tables

Key tables:
- message_history: Full raw messages (message_id, channel_id, guild_id, author_id, author_nickname, is_bot, content, timestamp, attachment_urls, reply_to_id)
  - message_id is the Discord message ID - you can make jump links: https://discord.com/channels/{{guild_id}}/{{channel_id}}/{{message_id}}
"""


class ClaudeCliError(Exception):
    """Base exception for Claude CLI errors."""


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
    """Get allowedTools and disallowedTools based on channel mode."""
    mode = channel_config.get("mode", "full")
    channel_name = channel_config.get("_folder", channel_config.get("name", "default"))

    if mode == "chat":
        allowed = (
            f"Read,WebSearch,WebFetch,Bash,"
            f"Edit(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/channels/{channel_name}/**),"
            f"Edit(//data/wendy/claude_fragments/**),Write(//data/wendy/claude_fragments/**),"
            f"Write(//data/wendy/tmp/**),Write(//tmp/**)"
        )
    else:
        allowed = (
            f"Read,WebSearch,WebFetch,Bash,"
            f"Edit(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/channels/{channel_name}/**),"
            f"Edit(//data/wendy/claude_fragments/**),Write(//data/wendy/claude_fragments/**),"
            f"Write(//data/wendy/tmp/**),Write(//tmp/**)"
        )

    disallowed = "Edit(//app/**),Write(//app/**)"

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
) -> list[str]:
    """Build the Claude CLI command with all flags."""
    cmd = [
        cli_path,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
    ]

    if fork_mode:
        cmd.extend(["--resume", session_id, "--fork-session"])
    elif is_new_session:
        cmd.extend(["--session-id", session_id])
    else:
        cmd.extend(["--resume", session_id])

    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    allowed_tools, disallowed_tools = get_permissions_for_channel(channel_config)
    cmd.extend(["--allowedTools", allowed_tools, "--disallowedTools", disallowed_tools])

    return cmd


def build_nudge_prompt(channel_id: int, is_thread: bool = False, thread_name: str | None = None) -> str:
    """Build the nudge prompt sent to Claude CLI via stdin."""
    if is_thread:
        return (
            f'<you\'ve been forked into a Discord thread: "{thread_name}". '
            f"Your conversation history from the parent channel has been preserved. "
            f"You MUST call curl -s http://localhost:{PROXY_PORT}/api/check_messages/{channel_id} "
            f"before any other action. Do not assume what the messages contain.>"
        )
    return (
        f"<new messages - you MUST call curl -s http://localhost:{PROXY_PORT}/api/check_messages/{channel_id} "
        f"before any other action. Do not assume what the messages contain.>"
    )


def setup_channel_folder(channel_name: str, beads_enabled: bool = False) -> None:
    """Create channel-specific folder and copy Claude Code settings."""
    ensure_channel_dirs(channel_name, beads_enabled=beads_enabled)
    chan_dir = channel_dir(channel_name)

    claude_settings_src = Path("/app/config/claude_settings.json")
    claude_dir = chan_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_dest = claude_dir / "settings.json"
    if claude_settings_src.exists():
        if not settings_dest.exists() or settings_dest.stat().st_mtime < claude_settings_src.stat().st_mtime:
            shutil.copy2(claude_settings_src, settings_dest)


def setup_wendy_scripts() -> None:
    """Ensure shell scripts are available and shared dirs exist."""
    scripts_src = Path("/app/scripts")
    if scripts_src.exists():
        for script in scripts_src.glob("*.sh"):
            dest = WENDY_BASE / script.name
            if not dest.exists() or dest.stat().st_mtime < script.stat().st_mtime:
                shutil.copy2(script, dest)
                dest.chmod(0o755)
        for script in scripts_src.glob("*.py"):
            dest = WENDY_BASE / script.name
            if not dest.exists() or dest.stat().st_mtime < script.stat().st_mtime:
                shutil.copy2(script, dest)

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
    """Read the most recent Claude CLI debug file for error messages."""
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

        lines = content.strip().split("\n")
        for line in reversed(lines[-20:]):
            if "[ERROR]" in line:
                if "Error:" in line:
                    return line.split("Error:", 1)[-1].strip()[:200]
                return line.split("[ERROR]", 1)[-1].strip()[:200]

    except Exception as e:
        _LOG.warning("Failed to read CLI debug files: %s", e)
    return None


def extract_forked_session_id(events: list[dict], session_cwd_folder: str) -> str | None:
    """Extract the forked session ID from stream-json events."""
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


async def run_cli(
    channel_id: int,
    channel_config: dict,
    system_prompt: str,
    model_override: str | None = None,
    force_new_session: bool = False,
) -> None:
    """Spawn Claude CLI, stream output, and track session state.

    This is the main entry point for running Claude CLI.
    Wendy's responses go through the send_message API, not stdout.
    """
    cli_path = find_cli_path()
    channel_name = channel_config.get("_folder", channel_config.get("name", "default"))
    beads_enabled = channel_config.get("beads_enabled", False)

    is_thread = channel_config.get("_is_thread", False)
    parent_folder = channel_config.get("_parent_folder")
    thread_name = channel_config.get("_thread_name")

    # For threads, sessions live in the parent's project directory
    session_cwd_folder = parent_folder if (is_thread and parent_folder) else channel_name

    # Get or create session
    session_info = sessions.get_session(channel_id)
    channel_changed = (
        session_info is not None
        and session_info.folder != session_cwd_folder
    )
    if channel_changed:
        _LOG.warning("Channel folder changed for %d: %s -> %s", channel_id, session_info.folder, session_cwd_folder)

    is_new_session = session_info is None or force_new_session or channel_changed

    # For new thread sessions, try to fork from parent
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
                _LOG.info("Thread fork: --resume %s --fork-session from parent %s",
                          session_id[:8], parent_folder)

    if is_new_session and not fork_mode:
        session_id = sessions.create_session(channel_id, session_cwd_folder)
    elif not is_new_session:
        session_id = session_info.session_id

    # Resolve model
    effective_model = resolve_model(model_override) if model_override else resolve_model(
        channel_config.get("model")
    )

    cmd = build_cli_command(
        cli_path, session_id, is_new_session, system_prompt,
        channel_config, effective_model, fork_mode=fork_mode,
    )

    nudge_prompt = build_nudge_prompt(
        channel_id, is_thread=is_thread, thread_name=thread_name,
    )

    # Setup
    WENDY_BASE.mkdir(parents=True, exist_ok=True)
    setup_wendy_scripts()
    setup_channel_folder(channel_name, beads_enabled=beads_enabled)

    session_action = "starting new" if is_new_session else "resuming"
    _LOG.info("CLI: %s session %s for channel %d (model=%s)", session_action, session_id[:8], channel_id, effective_model)

    proc = None
    timeout = CLAUDE_CLI_TIMEOUT
    try:
        cli_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
        if beads_enabled:
            cli_env["BEADS_DIR"] = str(beads_dir(channel_name))

        channel_cwd = channel_dir(session_cwd_folder)

        # Write session ID for orchestrator forking
        if beads_enabled:
            cs_file = current_session_file(channel_name)
            try:
                temp_file = cs_file.with_suffix(".tmp")
                temp_file.write_text(session_id)
                temp_file.replace(cs_file)
            except Exception as e:
                _LOG.warning("Failed to write current session file: %s", e)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
            cwd=channel_cwd,
            env=cli_env,
        )

        proc.stdin.write(nudge_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        events: list[dict] = []
        usage: dict[str, Any] = {}

        async def read_stream():
            nonlocal usage
            async for line in proc.stdout:
                decoded = line.decode("utf-8").strip()
                if not decoded:
                    continue
                try:
                    event = json.loads(decoded)
                    events.append(event)
                    append_to_stream_log(event, channel_id)
                    if event.get("type") == "result":
                        usage = event.get("usage", {})
                except json.JSONDecodeError:
                    continue

        try:
            await asyncio.wait_for(read_stream(), timeout=timeout)
        except TimeoutError:
            _LOG.error("CLI stdout read timed out after %ds", timeout)
            raise

        await proc.wait()

        stderr_data = await proc.stderr.read()
        stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

        if proc.returncode != 0:
            _LOG.error("CLI failed: %s", stderr_text)
            session_error = (
                "--resume" in cmd and (
                    "session" in stderr_text.lower()
                    or "no conversation found" in stderr_text.lower()
                    or not stderr_text.strip()
                )
            )
            if session_error and not force_new_session:
                _LOG.warning("Session resume failed, retrying with fresh session for channel %d", channel_id)
                return await run_cli(
                    channel_id, channel_config, system_prompt,
                    model_override=model_override, force_new_session=True,
                )

            error_detail = stderr_text or get_recent_cli_error() or "unknown error"
            raise ClaudeCliError(f"CLI failed (code {proc.returncode}): {error_detail}")

        save_debug_log(events, channel_id)
        trim_stream_log()

        # Capture forked session ID for threads
        if fork_mode:
            forked_id = extract_forked_session_id(events, session_cwd_folder)
            if forked_id:
                sessions.create_session(channel_id, session_cwd_folder, session_id=forked_id)
                _LOG.info("Thread fork complete: parent=%s -> forked=%s", session_id[:8], forked_id[:8])

        if usage:
            sessions.update_stats(channel_id, usage)

        _LOG.info("CLI: completed, events_streamed=%d", len(events))

    except TimeoutError:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        raise ClaudeCliError(f"Timed out after {timeout}s") from None
