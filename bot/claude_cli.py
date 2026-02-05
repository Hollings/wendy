"""Text-generation backend using Claude CLI for subscription-based usage.

This generator invokes the `claude` CLI command instead of the Anthropic API,
allowing use of subscription usage instead of API credits for cost savings.

Uses --resume for persistent per-channel sessions, with simple nudge prompts
to trigger Wendy to check messages via her tools.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .paths import (
    SHARED_DIR,
    WENDY_BASE,
    beads_dir,
    channel_dir,
    claude_md_path,
    current_session_file,
    ensure_channel_dirs,
    ensure_shared_dirs,
    session_dir,
)
from .state_manager import state as state_manager

_LOG = logging.getLogger(__name__)

# Legacy path for one-time migration
_SESSION_STATE_FILE_LEGACY: Path = Path("/data/wendy/session_state.json")
"""Legacy JSON file path - used only for one-time migration to SQLite."""

# Session directories are per-channel: /root/.claude/projects/-data-wendy-channels-{name}/
# e.g., /data/wendy/channels/coding -> /root/.claude/projects/-data-wendy-channels-coding/

STREAM_LOG_FILE: Path = WENDY_BASE / "stream.jsonl"
"""Rolling log file for real-time event streaming from Claude CLI."""

# Limits for session management
MAX_DISCORD_MESSAGES: int = 50
"""Maximum Discord messages to keep in a session before truncating older messages."""

MAX_STREAM_LOG_LINES: int = 5000
"""Maximum lines to keep in the rolling stream log file."""

# Sensitive env vars to filter from CLI subprocess
SENSITIVE_ENV_VARS: set[str] = {
    "DISCORD_TOKEN",
    "WEBHOOK_URL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "REPLICATE_API_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "WENDY_DEPLOY_TOKEN",
    "WENDY_GAMES_TOKEN",
}
"""Environment variables to exclude when spawning Claude CLI subprocesses.

These are filtered out to prevent the CLI from accessing sensitive credentials
that should only be available to the parent bot process.
"""

# Tool instructions template - {channel_id} and {channel_name} are substituted
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
   curl -X POST http://localhost:8945/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "your message here"}}'

   With attachment (file can be anywhere under /data/wendy/ or /tmp/):
   curl -X POST http://localhost:8945/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "check this out", "attachment": "/data/wendy/channels/{channel_name}/output.png"}}'

   This is the ONLY way to send messages to users. Your final output goes nowhere.

2. CHECK FOR NEW MESSAGES (optional, use before responding):
   curl -s http://localhost:8945/api/check_messages/{channel_id}

   Shows the last 10 messages to see if anyone sent new messages while you were thinking.
   Note: Always use -s flag with curl for cleaner output.

WORKFLOW:
1. Read/process the user's request
2. Do any work needed (read files, search, etc.)
3. ALWAYS call the send_message API to reply (unless explicitly told not to)
4. You can send multiple messages if needed

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
You can edit /data/wendy/channels/{channel_name}/CLAUDE.md to customize your own behavior. Anything you write there becomes part of your system instructions on the next message. Use this to remember things, set personal preferences, or adjust how you behave. Changes take effect immediately - no restart needed.

MESSAGE HISTORY DATABASE:
You have full read access to the message history at /data/wendy/shared/wendy.db. Use query_db.py to search messages, check past conversations, or find old content.

Usage:
  python3 /app/scripts/query_db.py "SELECT * FROM message_history WHERE content LIKE '%keyword%' LIMIT 20"
  python3 /app/scripts/query_db.py --schema    # Show all tables

Key tables:
- message_history: Full raw messages (message_id, channel_id, guild_id, author_id, author_nickname, is_bot, content, timestamp, attachment_urls, reply_to_id)
  - message_id is the Discord message ID - you can make jump links: https://discord.com/channels/{{guild_id}}/{{channel_id}}/{{message_id}}
"""


def _count_discord_messages_in_tool_result(content: str) -> int:
    """Count Discord messages in a check_messages tool result.

    The check_messages API returns JSON like:
    [{"message_id":123,"author":"user","content":"...","timestamp":123}]

    Returns the number of messages in the response, or 0 if not a check_messages result.
    """
    try:
        # Try to parse as JSON array of messages
        data = json.loads(content)
        if isinstance(data, list):
            # Check if it looks like Discord messages (has message_id and author)
            if data and isinstance(data[0], dict) and "message_id" in data[0] and "author" in data[0]:
                return len(data)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        pass
    return 0


def _count_discord_messages(messages: list) -> int:
    """Count actual Discord messages in a session by parsing check_messages tool results."""
    count = 0
    for msg in messages:
        if msg.get("type") != "user":
            continue
        # Look for tool_result content
        content_list = msg.get("message", {}).get("content", [])
        if not isinstance(content_list, list):
            continue
        for content_item in content_list:
            if content_item.get("type") == "tool_result":
                result_content = content_item.get("content", "")
                count += _count_discord_messages_in_tool_result(result_content)
    return count


class ClaudeCliError(Exception):
    """Base exception for Claude CLI errors."""
    pass


class ClaudeCliTextGenerator:
    """Generate text using Claude CLI for subscription-based usage.

    This generator invokes the `claude` CLI command instead of the Anthropic API,
    allowing use of Claude Code subscription quota instead of API credits for cost savings.

    The generator manages per-channel sessions with automatic:
    - Session persistence and resumption via --resume flag
    - Token usage tracking and statistics
    - Session truncation when Discord messages exceed MAX_DISCORD_MESSAGES
    - Temporary file management for image attachments
    - Debug logging and event streaming

    Attributes:
        model: The Claude model to use (e.g., "sonnet", "opus", "haiku").
        cli_path: Absolute path to the claude CLI executable.
        timeout: Maximum seconds to wait for CLI response before timing out.

    Example:
        >>> generator = ClaudeCliTextGenerator(model="sonnet")
        >>> await generator.generate(channel_id=123456789)
    """

    model: str
    cli_path: str
    timeout: int
    _temp_dir: Path | None
    _temp_files: list[Path]

    # Map shorthand names to explicit model IDs
    MODEL_MAP = {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-5-20250929",
        "haiku": "claude-haiku-4-5-20251001",
    }

    def __init__(self, model: str = "sonnet") -> None:
        """Initialize the Claude CLI text generator.

        Args:
            model: The Claude model identifier to use. Defaults to "sonnet".
                   Common values: "sonnet", "opus", "haiku".

        Raises:
            ClaudeCliError: If the claude CLI executable cannot be found.
        """
        self.model = self.MODEL_MAP.get(model, model)
        self.cli_path = self._find_cli_path()
        self.timeout = int(os.getenv("CLAUDE_CLI_TIMEOUT", "300"))
        self._temp_dir = None
        self._temp_files = []

        # One-time migration from legacy JSON to SQLite
        self._migrate_legacy_session_state()

    def _migrate_legacy_session_state(self) -> None:
        """One-time migration from JSON file to SQLite.

        Checks if the legacy JSON file exists and migrates data to SQLite.
        The file is renamed to .migrated after successful migration.
        """
        if _SESSION_STATE_FILE_LEGACY.exists():
            count = state_manager.migrate_from_session_json(_SESSION_STATE_FILE_LEGACY)
            if count > 0:
                _LOG.info("Migrated %d sessions from legacy JSON to SQLite", count)

    def _get_channel_session(self, channel_id: int) -> dict[str, Any] | None:
        """Retrieve session info for a specific Discord channel.

        Args:
            channel_id: The Discord channel ID to look up.

        Returns:
            Session info dict containing session_id, token counts, etc.,
            or None if no session exists for this channel.
        """
        return state_manager.get_session_stats(channel_id)

    def _create_channel_session(self, channel_id: int, channel_name: str) -> str:
        """Create a new Claude CLI session for a Discord channel.

        Args:
            channel_id: The Discord channel ID to create a session for.
            channel_name: The channel name (used as folder name).

        Returns:
            The newly generated UUID session ID.
        """
        session_id = str(uuid.uuid4())
        state_manager.create_session(channel_id, session_id, channel_name)
        _LOG.info("Created new session %s for channel %d (channel=%s)", session_id, channel_id, channel_name)
        return session_id

    def _update_session_stats(self, channel_id: int, usage: dict[str, Any]) -> None:
        """Update session statistics after a successful CLI run.

        Increments message count and accumulates token usage metrics.
        Also triggers session truncation check if messages exceed limits.

        Args:
            channel_id: The Discord channel ID whose session to update.
            usage: Token usage dict from CLI response containing input_tokens,
                   output_tokens, cache_read_input_tokens, cache_creation_input_tokens.
        """
        session_info = state_manager.get_session(channel_id)
        if not session_info:
            return

        state_manager.update_session_stats(
            channel_id,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_create_tokens=usage.get("cache_creation_input_tokens", 0),
        )

        # Check if session needs truncation (folder field stores channel_name)
        self._truncate_session_if_needed(session_info.session_id, session_info.folder)

    def _truncate_session_if_needed(self, session_id: str, channel_name: str) -> None:
        """Truncate session history if Discord messages exceed MAX_DISCORD_MESSAGES.

        This prevents sessions from growing indefinitely by removing older messages
        while preserving the most recent conversation context. The truncation is
        careful to avoid cutting in the middle of a tool_result to maintain
        conversation coherence.

        Args:
            session_id: The UUID session ID to check and potentially truncate.
            channel_name: The channel name (determines project directory).
        """
        # Claude CLI stores sessions in project directories based on cwd path
        # e.g., /data/wendy/channels/coding -> /root/.claude/projects/-data-wendy-channels-coding
        sess_dir = session_dir(channel_name)
        session_file = sess_dir / f"{session_id}.jsonl"
        if not session_file.exists():
            return

        try:
            # Read all messages
            messages = []
            with open(session_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            messages.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

            # Count actual Discord messages (from check_messages tool results)
            discord_msg_count = _count_discord_messages(messages)

            if discord_msg_count <= MAX_DISCORD_MESSAGES:
                return  # No truncation needed

            _LOG.info(
                "Session %s has %d Discord messages (max %d), truncating...",
                session_id[:8], discord_msg_count, MAX_DISCORD_MESSAGES
            )

            # Find cutoff point by walking backwards and counting Discord messages
            discord_msgs_seen = 0
            cutoff_idx = len(messages)

            for i in range(len(messages) - 1, -1, -1):
                msg = messages[i]
                if msg.get("type") == "user":
                    content_list = msg.get("message", {}).get("content", [])
                    if isinstance(content_list, list):
                        for content_item in content_list:
                            if content_item.get("type") == "tool_result":
                                result_content = content_item.get("content", "")
                                discord_msgs_seen += _count_discord_messages_in_tool_result(result_content)

                    if discord_msgs_seen >= MAX_DISCORD_MESSAGES:
                        cutoff_idx = i
                        break

            # Make sure we don't start with a tool_result
            while cutoff_idx < len(messages):
                msg = messages[cutoff_idx]
                if msg.get("type") == "user":
                    content = msg.get("message", {}).get("content", [])
                    if isinstance(content, list) and content:
                        if content[0].get("type") == "tool_result":
                            cutoff_idx += 1
                            continue
                break

            if cutoff_idx >= len(messages):
                _LOG.warning("Session %s: couldn't find clean truncation point", session_id[:8])
                return

            truncated = messages[cutoff_idx:]
            removed_count = len(messages) - len(truncated)

            with open(session_file, "w") as f:
                for msg in truncated:
                    f.write(json.dumps(msg) + "\n")

            _LOG.info(
                "Truncated session %s: removed %d entries, kept %d (with %d Discord messages)",
                session_id[:8], removed_count, len(truncated),
                _count_discord_messages(truncated)
            )

        except Exception as e:
            _LOG.error("Failed to truncate session %s: %s", session_id[:8], e)

    def get_session_stats(self, channel_id: int) -> dict[str, Any] | None:
        """Get session stats for a channel."""
        return state_manager.get_session_stats(channel_id)

    def reset_channel_session(self, channel_id: int, channel_name: str = "default") -> str:
        """Reset a channel's session, return new session_id."""
        return self._create_channel_session(channel_id, channel_name)

    def _find_cli_path(self) -> str:
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

        raise ClaudeCliError(
            "Claude CLI not found. Install it or set CLAUDE_CLI_PATH env var."
        )

    def _get_recent_cli_error(self) -> str | None:
        """Read the most recent Claude CLI debug file to extract error messages."""
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
                if match:
                    return match.group(1)
                return "authentication error"

            lines = content.strip().split("\n")
            for line in reversed(lines[-20:]):
                if "[ERROR]" in line:
                    if "Error:" in line:
                        return line.split("Error:", 1)[-1].strip()[:200]
                    return line.split("[ERROR]", 1)[-1].strip()[:200]

            return None
        except Exception as e:
            _LOG.warning("Failed to read CLI debug files: %s", e)
            return None

    def _ensure_temp_dir(self) -> Path:
        """Create temp directory for images if needed."""
        if self._temp_dir is None or not self._temp_dir.exists():
            self._temp_dir = Path(tempfile.mkdtemp(prefix="claude_cli_"))
        return self._temp_dir

    def _save_images_to_temp(self, images: list[dict[str, Any]]) -> list[Path]:
        """Save base64 images to Wendy's images folder."""
        paths = []
        images_dir = SHARED_DIR / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        _LOG.info("Processing %d images for CLI", len(images))

        for i, img in enumerate(images):
            data_url = img.get("data_url", "")
            if not data_url or not data_url.startswith("data:"):
                continue

            try:
                header, b64_data = data_url.split(",", 1)
                media_type = header.split(";")[0].replace("data:", "")
                ext = media_type.split("/")[-1]
                if ext == "jpeg":
                    ext = "jpg"

                image_bytes = base64.b64decode(b64_data)
                image_path = images_dir / f"img_{i}_{int(time.time() * 1000)}.{ext}"
                image_path.write_bytes(image_bytes)
                paths.append(image_path)
                _LOG.info("Saved image to %s (%d bytes)", image_path, len(image_bytes))
            except Exception as e:
                _LOG.exception("Failed to save image %d: %s", i, e)

        return paths

    def _format_image_references(self, images: list[dict[str, Any]]) -> str:
        """Format image references for the prompt."""
        paths = self._save_images_to_temp(images)
        if not paths:
            return ""

        refs = [f"[Image: {p}]" for p in paths]
        return "\n".join(refs)

    def _cleanup_temp_files(self) -> None:
        """Clean up temporary image files."""
        for path in self._temp_files:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        self._temp_files.clear()

        if self._temp_dir and self._temp_dir.exists():
            try:
                self._temp_dir.rmdir()
            except OSError:
                pass

    def _setup_wendy_scripts(self) -> None:
        """Ensure Wendy's shell scripts are available and shared dirs exist."""
        scripts_src = Path("/app/scripts")

        # Copy scripts to base wendy dir for easy access
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

        # Ensure shared directories exist
        ensure_shared_dirs()

        # Ensure secrets directory exists (at base level for shared access)
        secrets_dir = WENDY_BASE / "secrets"
        secrets_dir.mkdir(exist_ok=True, mode=0o700)

    def _setup_channel_folder(self, channel_name: str, beads_enabled: bool = False) -> None:
        """Create channel-specific folder if it doesn't exist.

        Args:
            channel_name: The channel name (used as folder name).
            beads_enabled: If True, create the .beads directory and copy BD_USAGE.md.
        """
        # Ensure channel directory exists
        ensure_channel_dirs(channel_name, beads_enabled=beads_enabled)
        chan_dir = channel_dir(channel_name)

        # Set up Claude Code settings (hooks to block Task tool)
        claude_settings_src = Path("/app/config/claude_settings.json")
        claude_dir = chan_dir / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings_dest = claude_dir / "settings.json"
        if claude_settings_src.exists():
            if not settings_dest.exists() or settings_dest.stat().st_mtime < claude_settings_src.stat().st_mtime:
                shutil.copy2(claude_settings_src, settings_dest)

        # Copy BD_USAGE.md to channels with beads enabled
        if beads_enabled:
            bd_usage_src = Path("/app/config/BD_USAGE.md")
            bd_usage_dest = chan_dir / "BD_USAGE.md"
            if bd_usage_src.exists():
                if not bd_usage_dest.exists() or bd_usage_dest.stat().st_mtime < bd_usage_src.stat().st_mtime:
                    shutil.copy2(bd_usage_src, bd_usage_dest)

    def _get_wendys_notes(self, channel_name: str) -> str:
        """Load Wendy's self-editable notes from her personal CLAUDE.md."""
        notes_path = claude_md_path(channel_name)
        if not notes_path.exists():
            return ""
        try:
            content = notes_path.read_text().strip()
            if content:
                return f"\n\n---\nYOUR PERSONAL NOTES (from channels/{channel_name}/CLAUDE.md - you can edit this!):\n{content}\n---"
            return ""
        except Exception as e:
            _LOG.warning("Failed to read Wendy's notes: %s", e)
            return ""

    def _get_tool_instructions(self, channel_id: int, channel_name: str) -> str:
        """Get instructions for Wendy's API tools."""
        return TOOL_INSTRUCTIONS_TEMPLATE.format(channel_id=channel_id, channel_name=channel_name)

    def _parse_stream_json(self, output: str, channel_id: int | None = None) -> str:
        """Parse stream-json output from Claude CLI and save debug log."""
        events = []
        result_text = ""

        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                events.append(event)

                if event.get("type") == "result":
                    result_text = event.get("result", "")

            except json.JSONDecodeError as e:
                _LOG.warning("Failed to parse stream-json line: %s", e)
                continue

        self._save_debug_log(events, channel_id)
        return result_text

    def _save_debug_log(self, events: list[dict], channel_id: int | None) -> None:
        """Save CLI events to debug log file."""
        try:
            debug_dir = Path("/data/wendy/debug_logs")
            debug_dir.mkdir(parents=True, exist_ok=True)

            timestamp = int(time.time() * 1000)
            channel_str = str(channel_id) if channel_id else "unknown"
            log_path = debug_dir / f"{channel_str}_{timestamp}.json"

            debug_data = {
                "timestamp": timestamp,
                "channel_id": channel_id,
                "events": events,
                "summary": self._summarize_events(events),
            }

            log_path.write_text(json.dumps(debug_data, indent=2))
            _LOG.info("Saved CLI debug log to %s", log_path)

            logs = sorted(debug_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old_log in logs[:-20]:
                old_log.unlink()

        except Exception as e:
            _LOG.error("Failed to save debug log: %s", e)

    def _summarize_events(self, events: list[dict]) -> dict:
        """Extract summary info from events for quick debugging."""
        summary = {
            "tool_uses": [],
            "assistant_messages": [],
            "total_cost_usd": None,
            "num_turns": None,
        }

        for event in events:
            event_type = event.get("type")

            if event_type == "assistant":
                msg = event.get("message", {})
                content = msg.get("content", [])
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            summary["tool_uses"].append({
                                "tool": block.get("name"),
                                "input_preview": str(block.get("input", ""))[:200],
                            })
                        elif block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                summary["assistant_messages"].append(text[:500])

            elif event_type == "result":
                summary["total_cost_usd"] = event.get("total_cost_usd")
                summary["num_turns"] = event.get("num_turns")

        return summary

    def _append_to_stream_log(self, event: dict, channel_id: int | None) -> None:
        """Append a single event to the rolling stream log file."""
        try:
            STREAM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

            enriched_event = {
                "ts": int(time.time() * 1000),
                "channel_id": str(channel_id) if channel_id else None,
                "event": event,
            }

            with open(STREAM_LOG_FILE, "a") as f:
                f.write(json.dumps(enriched_event) + "\n")

        except Exception as e:
            _LOG.error("Failed to append to stream log: %s", e)

    def _trim_stream_log_if_needed(self) -> None:
        """Trim stream log to MAX_STREAM_LOG_LINES if it gets too large."""
        try:
            if not STREAM_LOG_FILE.exists():
                return

            with open(STREAM_LOG_FILE) as f:
                lines = f.readlines()

            if len(lines) > MAX_STREAM_LOG_LINES:
                with open(STREAM_LOG_FILE, "w") as f:
                    f.writelines(lines[-MAX_STREAM_LOG_LINES:])
                _LOG.info("Trimmed stream log from %d to %d lines", len(lines), MAX_STREAM_LOG_LINES)
        except Exception as e:
            _LOG.error("Failed to trim stream log: %s", e)

    def _build_system_prompt(self, channel_id: int, channel_name: str, mode: str, beads_enabled: bool) -> str:
        """Build the complete system prompt for a channel."""
        prompt = self._get_base_system_prompt(channel_name, mode)
        prompt += self._get_wendys_notes(channel_name)
        prompt += self._get_tool_instructions(channel_id, channel_name)
        if beads_enabled:
            prompt += self._get_active_beads_warning(channel_name)
        return prompt

    def _build_cli_command(
        self,
        session_id: str,
        is_new_session: bool,
        system_prompt: str,
        channel_config: dict,
        model: str = None,
    ) -> list[str]:
        """Build the Claude CLI command with all flags."""
        cmd = [
            self.cli_path,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model or self.model,
        ]

        if is_new_session:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--resume", session_id])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        allowed_tools, disallowed_tools = self._get_permissions_for_channel(channel_config)
        cmd.extend([
            "--allowedTools", allowed_tools,
            "--disallowedTools", disallowed_tools,
        ])

        return cmd

    def _get_permissions_for_channel(self, channel_config: dict) -> tuple[str, str]:
        """Get allowedTools and disallowedTools based on channel mode.

        Args:
            channel_config: Channel config dict with 'mode' and 'name' keys

        Returns:
            Tuple of (allowedTools, disallowedTools) strings
        """
        mode = channel_config.get("mode", "full")
        # Use _folder for backwards compat, or fall back to name
        channel_name = channel_config.get("_folder", channel_config.get("name", "default"))

        if mode == "chat":
            # Chat mode: restricted access, no beads, can only edit own channel folder
            # Files can be sent from anywhere under /data/wendy/ or /tmp/
            allowed = f"Read,WebSearch,WebFetch,Bash,Edit(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/tmp/**),Write(//tmp/**)"
            # Block access to other channel folders and system files
            disallowed = "Edit(//data/wendy/*.sh),Edit(//data/wendy/*.py),Edit(//app/**),Write(//app/**)"
        else:
            # Full mode: full access to channel folder
            allowed = f"Read,WebSearch,WebFetch,Bash,Edit(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/tmp/**),Write(//tmp/**)"
            disallowed = "Edit(//app/**),Write(//app/**)"

        return allowed, disallowed

    async def generate(
        self,
        channel_id: int,
        channel_config: dict = None,
        model_override: str = None,
        **kwargs,
    ) -> str:
        """Generate response using Claude CLI with persistent sessions.

        Uses --resume for per-channel sessions. Sends a simple nudge for Wendy
        to check messages via her tools.

        Args:
            channel_id: Discord channel ID (required for session management)
            channel_config: Optional channel config dict with mode, name, beads_enabled
            model_override: Optional model to use instead of default (e.g., "haiku" for webhooks)

        Returns:
            Empty string (Wendy's responses go through send_message API)
        """
        if not channel_id:
            raise ValueError("channel_id is required for Claude CLI sessions")

        # Default config if not provided
        if channel_config is None:
            channel_config = {"mode": "full", "name": "default", "beads_enabled": False}

        # Use _folder for backwards compat, or fall back to name
        channel_name = channel_config.get("_folder", channel_config.get("name", "default"))
        mode = channel_config.get("mode", "full")
        beads_enabled = channel_config.get("beads_enabled", False)

        # Get or create session for this channel
        force_new = kwargs.get("_force_new_session", False)
        session_info = self._get_channel_session(channel_id)

        # Check if channel name changed - if so, we need a new session since Claude CLI
        # stores sessions per-project (based on cwd)
        channel_changed = (
            session_info is not None
            and session_info.get("folder") != channel_name
        )
        if channel_changed:
            _LOG.warning(
                "Channel changed for channel %d: %s -> %s, creating new session",
                channel_id, session_info.get("folder"), channel_name
            )

        is_new_session = session_info is None or force_new or channel_changed

        if is_new_session:
            session_id = self._create_channel_session(channel_id, channel_name)
        else:
            session_id = session_info["session_id"]

        # Build system prompt and CLI command
        effective_model = self.MODEL_MAP.get(model_override, model_override) if model_override else self.model
        system_prompt = self._build_system_prompt(channel_id, channel_name, mode, beads_enabled)
        cmd = self._build_cli_command(session_id, is_new_session, system_prompt, channel_config, model=effective_model)

        session_action = "starting new" if is_new_session else "resuming"
        _LOG.info("ClaudeCLI: %s session %s for channel %d (model=%s)",
                  session_action, session_id[:8], channel_id, effective_model)

        nudge_prompt = f"<new messages - you MUST call curl -s http://localhost:8945/api/check_messages/{channel_id} before any other action. Do not assume what the messages contain.>"

        # Ensure base and shared directories exist
        WENDY_BASE.mkdir(parents=True, exist_ok=True)
        self._setup_wendy_scripts()
        self._setup_channel_folder(channel_name, beads_enabled=beads_enabled)

        proc = None
        try:
            cli_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}

            # Set BEADS_DIR for channels with beads enabled so bd command can find .beads
            # Note: BEADS_DIR must point to the .beads directory itself, not the project root
            if beads_enabled:
                cli_env["BEADS_DIR"] = str(beads_dir(channel_name))

            # Use channel-specific folder as cwd for isolation
            channel_cwd = channel_dir(channel_name)

            # Write session ID to .current_session for orchestrator to fork
            # Only for channels with beads enabled
            if beads_enabled:
                session_file = current_session_file(channel_name)
                try:
                    # Write to temp file then atomically rename
                    temp_file = session_file.with_suffix(".tmp")
                    temp_file.write_text(session_id)
                    temp_file.replace(session_file)
                    _LOG.debug("Wrote session ID %s to %s", session_id[:8], session_file)
                except Exception as e:
                    _LOG.warning("Failed to write current session file: %s", e)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,  # 10MB line buffer
                cwd=channel_cwd,
                env=cli_env,
            )

            proc.stdin.write(nudge_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

            events = []
            result_text = ""
            usage = {}

            async def read_stream_with_timeout():
                nonlocal result_text, usage
                async for line in proc.stdout:
                    line = line.decode("utf-8").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        events.append(event)
                        self._append_to_stream_log(event, channel_id)

                        if event.get("type") == "result":
                            result_text = event.get("result", "")
                            usage = event.get("usage", {})

                    except json.JSONDecodeError as e:
                        _LOG.warning("Failed to parse stream-json line: %s", e)
                        continue

            try:
                await asyncio.wait_for(read_stream_with_timeout(), timeout=self.timeout)
            except TimeoutError:
                _LOG.error("Claude CLI stdout read timed out after %ds", self.timeout)
                raise

            await proc.wait()

            stderr_data = await proc.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

            if proc.returncode != 0:
                _LOG.error("Claude CLI failed: %s", stderr_text)
                session_error = (
                    "--resume" in cmd and (
                        "session" in stderr_text.lower() or
                        "no conversation found" in stderr_text.lower() or
                        not stderr_text.strip()
                    )
                )
                if session_error and not kwargs.get("_force_new_session"):
                    _LOG.warning("Session resume failed, retrying with fresh session for channel %d", channel_id)
                    return await self.generate(channel_id, _force_new_session=True, **kwargs)

                error_detail = stderr_text
                if not stderr_text.strip():
                    error_detail = self._get_recent_cli_error() or "unknown error"

                raise ClaudeCliError(
                    f"CLI failed (code {proc.returncode}): {error_detail}"
                )

            self._save_debug_log(events, channel_id)
            self._trim_stream_log_if_needed()

            if usage:
                self._update_session_stats(channel_id, usage)

            _LOG.info("ClaudeCLI: response_len=%d, events_streamed=%d", len(result_text), len(events))
            return ""

        except TimeoutError as e:
            _LOG.error("Claude CLI timed out after %ds", self.timeout)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise ClaudeCliError(f"Timed out after {self.timeout}s") from e
        finally:
            self._cleanup_temp_files()

    def _get_base_system_prompt(self, channel_name: str, mode: str = "full") -> str:
        """Get the base system prompt for Wendy.

        Args:
            channel_name: The channel name for path substitution
            mode: 'chat' for limited permissions, 'full' for coding channel
        """
        system_prompt_file = os.getenv("SYSTEM_PROMPT_FILE", "/app/config/system_prompt.txt")
        if not Path(system_prompt_file).exists():
            return ""

        try:
            content = Path(system_prompt_file).read_text().strip()

            # Replace folder placeholder with channel name
            content = content.replace("{folder}", channel_name)

            # For chat mode, strip out deployment and task system instructions
            if mode == "chat":
                # Remove "Writing code and tasks" through "Progress updates" sections
                # Remove "Deployment" section entirely (to end of file)
                lines = content.split("\n")
                filtered_lines = []
                skip_until_section = None
                skip_to_end = False

                for line in lines:
                    # Check for section headers we want to skip in chat mode
                    if line.strip() == "Writing code and tasks":
                        skip_until_section = "Progress updates"
                        continue
                    if line.strip() == "Deployment":
                        skip_to_end = True  # Skip everything from here to end
                        continue

                    # If we found the end of a skipped section, stop skipping
                    if skip_until_section and line.strip() == skip_until_section:
                        skip_until_section = None
                        # Keep this line (it's the start of an included section)

                    # Skip lines while in a skipped section or skipping to end
                    if skip_until_section or skip_to_end:
                        continue

                    filtered_lines.append(line)

                content = "\n".join(filtered_lines)

            return content
        except Exception as e:
            _LOG.warning("Failed to read system prompt file: %s", e)
            return ""

    def _get_active_beads_warning(self, channel_name: str) -> str:
        """Check for in-progress beads and return a warning if any are active."""
        try:
            # Read directly from beads JSONL file instead of shelling out
            jsonl_path = beads_dir(channel_name) / "issues.jsonl"
            if not jsonl_path.exists():
                return ""

            # Parse JSONL - later lines update earlier ones (append-only log)
            issues_by_id = {}
            for line in jsonl_path.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    issue_id = data.get("id")
                    if issue_id:
                        issues_by_id[issue_id] = data
                except json.JSONDecodeError:
                    continue

            # Filter to in_progress only
            tasks = [
                t for t in issues_by_id.values()
                if t.get("status") == "in_progress"
            ]

            if not tasks:
                return ""

            # Build warning message
            task_list = "\n".join([
                f"  - {t.get('id', '?')}: {t.get('title', 'Untitled')}"
                for t in tasks
            ])
            return f"""

---
WARNING: You have {len(tasks)} task(s) currently in progress:
{task_list}

Do NOT start new tasks until these are resolved. Check on them or mark them complete/cancelled first.
Use `bd status <id>` to check status or `bd close <id>` to complete a task.
---
"""

        except Exception as e:
            _LOG.warning("Failed to check active beads: %s", e)
            return ""
