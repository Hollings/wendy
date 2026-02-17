"""Session lifecycle: create, resume, truncate, recover.

Dedicated module for per-channel Claude CLI session management.
"""
from __future__ import annotations

import json
import logging
import uuid

from .config import MAX_DISCORD_MESSAGES
from .paths import session_dir
from .state import state as state_manager

_LOG = logging.getLogger(__name__)


def create_session(channel_id: int, folder: str, session_id: str | None = None) -> str:
    """Create a new session for a channel. Returns the session ID."""
    sid = session_id or str(uuid.uuid4())
    state_manager.create_session(channel_id, sid, folder)
    return sid


def get_session(channel_id: int):
    """Get existing session info, or None."""
    return state_manager.get_session(channel_id)


def reset_session(channel_id: int, folder: str) -> str:
    """Reset a channel's session. Returns new session ID."""
    return create_session(channel_id, folder)


def update_stats(channel_id: int, usage: dict) -> None:
    """Update session stats after a CLI run and check for truncation."""
    session = state_manager.get_session(channel_id)
    if not session:
        return

    state_manager.update_session_stats(
        channel_id,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_create_tokens=usage.get("cache_creation_input_tokens", 0),
    )

    truncate_if_needed(session.session_id, session.folder)


# =============================================================================
# Message counting
# =============================================================================


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


def _count_discord_messages(messages: list) -> int:
    """Count actual Discord messages in a session by parsing check_messages tool results."""
    count = 0
    for msg in messages:
        if msg.get("type") != "user":
            continue
        content_list = msg.get("message", {}).get("content", [])
        if not isinstance(content_list, list):
            continue
        for content_item in content_list:
            if content_item.get("type") == "tool_result":
                result_content = content_item.get("content", "")
                count += _count_discord_messages_in_tool_result(result_content)
    return count


# =============================================================================
# Truncation
# =============================================================================


def truncate_if_needed(session_id: str, channel_name: str) -> None:
    """Truncate session history if Discord messages exceed MAX_DISCORD_MESSAGES."""
    sess_dir = session_dir(channel_name)
    session_file = sess_dir / f"{session_id}.jsonl"
    if not session_file.exists():
        return

    try:
        messages = []
        with open(session_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        discord_msg_count = _count_discord_messages(messages)

        if discord_msg_count <= MAX_DISCORD_MESSAGES:
            return

        _LOG.info(
            "Session %s has %d Discord messages (max %d), truncating...",
            session_id[:8], discord_msg_count, MAX_DISCORD_MESSAGES
        )

        # Walk backwards counting Discord messages to find cutoff
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

        # Don't start with a tool_result
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

        temp_file = session_file.with_suffix(".tmp")
        with open(temp_file, "w") as f:
            for msg in truncated:
                f.write(json.dumps(msg) + "\n")
        temp_file.replace(session_file)

        _LOG.info(
            "Truncated session %s: removed %d entries, kept %d",
            session_id[:8], removed_count, len(truncated)
        )

    except Exception as e:
        _LOG.error("Failed to truncate session %s: %s", session_id[:8], e)
