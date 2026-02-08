"""Brain feed - real-time stream of Wendy's Claude Code session.

This module provides real-time streaming of Wendy's Claude Code session events
to connected dashboard clients via WebSocket. It watches the stream.jsonl file
for new events and broadcasts them to all authenticated clients.

Architecture:
    Claude CLI writes to stream.jsonl -> tail_stream() watches file
    -> broadcast() sends to WebSocket clients -> Dashboard displays

Features:
    - Efficient tail-reading of stream.jsonl from the end
    - Handles file truncation (when wendy-bot trims old events)
    - Tracks session stats (context usage, costs, active tasks)
    - Lists and reads subagent logs
    - Connection limit (MAX_CLIENTS) to prevent overload

Events:
    Events are JSON lines with structure: {"ts": "...", "event": {...}}
    Event types: assistant, user, result, tool_use, tool_result
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from fastapi import WebSocket
from watchfiles import Change, awatch

_LOG = logging.getLogger(__name__)

# =============================================================================
# Configuration Constants
# =============================================================================

STREAM_FILE: Path = Path("/data/wendy/stream.jsonl")
"""Path to the Claude Code stream.jsonl file."""

DB_PATH: Path = Path("/data/wendy/wendy.db")
"""Path to the SQLite database with cached messages and session state."""

CLAUDE_DIR: Path = Path("/data/claude")
"""Base directory for Claude Code data (projects, sessions)."""

MAX_HISTORY: int = 50
"""Number of recent events to send to newly connected clients."""

MAX_CLIENTS: int = 100
"""Maximum concurrent WebSocket connections allowed."""

CONTEXT_WINDOW: int = 200000
"""Claude's context window size in tokens (for percentage calculation)."""

# =============================================================================
# Module State
# =============================================================================

connected_clients: set[WebSocket] = set()
"""Set of currently connected WebSocket clients."""

_watcher_task: asyncio.Task | None = None
"""Background task running tail_stream()."""

_latest_stats: dict = {
    "context_tokens": 0,
    "context_pct": 0,
    "session_cost": 0.0,
    "last_activity": None,
    "active_tasks": 0,
}
"""Latest statistics extracted from stream events."""

_active_task_ids: set[str] = set()
"""Set of tool_use IDs for currently active Task calls."""


# =============================================================================
# Event Reading Functions
# =============================================================================


def get_recent_events(n: int = MAX_HISTORY) -> list[str]:
    """Get the last N events from stream file efficiently.

    Reads the file backwards from the end to avoid loading the entire
    file into memory (stream.jsonl can be several MB).

    Args:
        n: Maximum number of events to return.

    Returns:
        List of JSON event strings, newest last.
    """
    if not STREAM_FILE.exists():
        return []

    try:
        # Read file in reverse to get last N lines efficiently
        with open(STREAM_FILE, "rb") as f:
            # Seek to end
            f.seek(0, 2)
            file_size = f.tell()

            if file_size == 0:
                return []

            # Read chunks from end until we have enough lines
            chunk_size = 8192
            lines: list[str] = []
            position = file_size

            while position > 0 and len(lines) <= n:
                read_size = min(chunk_size, position)
                position -= read_size
                f.seek(position)
                chunk = f.read(read_size).decode("utf-8", errors="replace")

                # Split and accumulate lines
                chunk_lines = chunk.split("\n")
                if lines:
                    # Merge with previous partial line
                    chunk_lines[-1] += lines[0]
                    lines = chunk_lines + lines[1:]
                else:
                    lines = chunk_lines

            # Return last N non-empty lines
            return [line.strip() for line in lines[-n:] if line.strip()]

    except Exception as e:
        _LOG.error("Failed to read recent events: %s", e)
        return []


# =============================================================================
# WebSocket Broadcasting
# =============================================================================


async def broadcast(message: str) -> None:
    """Send a message to all connected WebSocket clients.

    Automatically removes dead connections that fail to receive.

    Args:
        message: JSON string to send to all clients.
    """
    if not connected_clients:
        return

    dead: set[WebSocket] = set()
    tasks = []

    for ws in connected_clients:
        try:
            tasks.append(ws.send_text(message))
        except Exception:
            dead.add(ws)

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ws, result in zip(list(connected_clients), results):
            if isinstance(result, Exception):
                dead.add(ws)

    connected_clients.difference_update(dead)

    if dead:
        _LOG.info("Removed %d dead connections, %d remaining", len(dead), len(connected_clients))


async def tail_stream() -> None:
    """Watch stream.jsonl and broadcast new events to all clients.

    Uses watchfiles to efficiently monitor the file for changes.
    Handles file truncation gracefully (when wendy-bot trims old events).
    Runs forever in a background task started by start_watcher().
    """
    _LOG.info("Starting brain feed watcher...")

    while True:
        try:
            # Wait for file to exist
            while not STREAM_FILE.exists():
                _LOG.debug("Waiting for stream file to exist...")
                await asyncio.sleep(5)

            pos = STREAM_FILE.stat().st_size
            _LOG.info("Stream file found, starting from position %d", pos)

            async for changes in awatch(STREAM_FILE):
                for change_type, _ in changes:
                    if change_type == Change.deleted:
                        _LOG.warning("Stream file deleted, waiting for recreation...")
                        break

                    try:
                        current_size = STREAM_FILE.stat().st_size

                        # Handle file truncation (wendy-bot trims to 5000 lines)
                        # Skip to current end and ignore this batch - the trim rewrites
                        # the whole file so we'd re-broadcast everything otherwise
                        if current_size < pos:
                            _LOG.info("File truncated, jumping to end (was %d, now %d)", pos, current_size)
                            pos = current_size
                            continue

                        if current_size <= pos:
                            continue

                        with open(STREAM_FILE) as f:
                            f.seek(pos)
                            new_lines = f.readlines()
                            pos = f.tell()

                        for line in new_lines:
                            line = line.strip()
                            if line:
                                update_stats_from_event(line)
                                await broadcast(line)

                    except FileNotFoundError:
                        _LOG.warning("Stream file disappeared during read")
                        pos = 0
                        break

        except Exception as e:
            _LOG.exception("Watcher error: %s", e)
            await asyncio.sleep(5)


# =============================================================================
# Client Management
# =============================================================================


async def add_client(ws: WebSocket) -> bool:
    """Add a WebSocket client connection.

    Args:
        ws: WebSocket connection to add.

    Returns:
        True if added successfully, False if at MAX_CLIENTS capacity.
    """
    if len(connected_clients) >= MAX_CLIENTS:
        return False
    connected_clients.add(ws)
    _LOG.info("Client connected, total: %d", len(connected_clients))
    return True


def remove_client(ws: WebSocket) -> None:
    """Remove a WebSocket client connection.

    Args:
        ws: WebSocket connection to remove.
    """
    connected_clients.discard(ws)
    _LOG.info("Client disconnected, total: %d", len(connected_clients))


def client_count() -> int:
    """Get the number of currently connected WebSocket clients.

    Returns:
        Number of active connections.
    """
    return len(connected_clients)


# =============================================================================
# Statistics Functions
# =============================================================================


def get_stats() -> dict:
    """Get current brain feed statistics.

    Combines real-time stats from stream events with data from
    session state and database.

    Returns:
        Dict with keys: viewers, context_tokens, context_pct, session_cost,
        last_activity, active_tasks, session_messages, cached_messages,
        session_id, total_input, total_output, cache_read.
    """
    stats = {
        "viewers": len(connected_clients),
        "context_tokens": _latest_stats["context_tokens"],
        "context_pct": _latest_stats["context_pct"],
        "session_cost": _latest_stats["session_cost"],
        "last_activity": _latest_stats["last_activity"],
        "active_tasks": _latest_stats["active_tasks"],
        "session_messages": 0,
        "cached_messages": 0,
    }

    # Get session state from SQLite
    try:
        if DB_PATH.exists():
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                # Get first channel's session stats
                row = conn.execute(
                    "SELECT * FROM channel_sessions ORDER BY last_used_at DESC LIMIT 1"
                ).fetchone()
                if row:
                    stats["session_messages"] = row["message_count"]
                    stats["session_id"] = row["session_id"][:8] if row["session_id"] else ""
                    stats["total_input"] = row["total_input_tokens"]
                    stats["total_output"] = row["total_output_tokens"]
                    stats["cache_read"] = row["total_cache_read_tokens"]
    except Exception as e:
        _LOG.debug("Failed to read session state: %s", e)

    # Get message history count
    try:
        if DB_PATH.exists():
            with sqlite3.connect(DB_PATH) as conn:
                count = conn.execute("SELECT COUNT(*) FROM message_history").fetchone()[0]
                stats["cached_messages"] = count  # Keep key for backwards compat
    except Exception as e:
        _LOG.debug("Failed to count message history: %s", e)

    return stats


def update_stats_from_event(event_json: str) -> None:
    """Update internal stats from a stream event.

    Parses the event JSON and updates _latest_stats with:
    - Context token usage (from assistant messages)
    - Session cost (from result events)
    - Active task count (increments on Task tool_use, decrements on tool_result)
    - Last activity timestamp

    Args:
        event_json: JSON string of the stream event.
    """
    global _latest_stats
    try:
        data = json.loads(event_json)
        event = data.get("event", {})
        ts = data.get("ts")

        if ts:
            _latest_stats["last_activity"] = ts

        # Track context usage from assistant messages
        if event.get("type") == "assistant":
            usage = event.get("message", {}).get("usage", {})
            if usage:
                # Calculate context load from cache read tokens
                cache_read = usage.get("cache_read_input_tokens", 0)
                input_tokens = usage.get("input_tokens", 0)
                _latest_stats["context_tokens"] = cache_read + input_tokens
                _latest_stats["context_pct"] = round(
                    (_latest_stats["context_tokens"] / CONTEXT_WINDOW) * 100, 1
                )

        # Track costs from result events
        if event.get("type") == "result":
            cost = event.get("total_cost_usd", 0)
            if cost:
                _latest_stats["session_cost"] = round(cost, 4)

        # Track active Task tool calls by ID
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_use" and block.get("name") == "Task":
                    tool_id = block.get("id")
                    if tool_id:
                        _active_task_ids.add(tool_id)
                        _latest_stats["active_tasks"] = len(_active_task_ids)

        # Remove completed tasks on matching tool results
        if event.get("type") == "user":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_result":
                    tool_id = block.get("tool_use_id")
                    if tool_id:
                        _active_task_ids.discard(tool_id)
                        _latest_stats["active_tasks"] = len(_active_task_ids)

    except Exception as e:
        _LOG.debug("Failed to parse event for stats: %s", e)


# =============================================================================
# Subagent Functions
# =============================================================================


def get_subagents_dir() -> Path | None:
    """Get the subagents directory for the current Claude session.

    Looks up the session ID from SQLite channel_sessions and scans
    Claude project directories matching -data-wendy-channels-* for
    the matching session's subagents directory.

    Returns:
        Path to subagents directory, or None if not found.
    """
    try:
        if not DB_PATH.exists():
            return None
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT session_id FROM channel_sessions ORDER BY last_used_at DESC LIMIT 1"
        ).fetchone()
        conn.close()

        if not row or not row["session_id"]:
            return None

        session_id = row["session_id"]
        projects_dir = CLAUDE_DIR / "projects"
        if not projects_dir.exists():
            return None

        for project_dir in projects_dir.glob("-data-wendy-channels-*"):
            subagents_dir = project_dir / session_id / "subagents"
            if subagents_dir.exists():
                return subagents_dir

        return None
    except Exception as e:
        _LOG.debug("Failed to get subagents dir: %s", e)
        return None


def list_agents() -> list[dict]:
    """List all subagent files with metadata.

    Scans the subagents directory for agent-*.jsonl files and extracts
    metadata from each (slug, task description, size, modified time).

    Returns:
        List of agent dicts sorted by modified time (newest first).
        Each dict has: id, slug, task, size, modified, path.
    """
    subagents_dir = get_subagents_dir()
    if not subagents_dir:
        return []

    agents = []
    try:
        for f in subagents_dir.glob("agent-*.jsonl"):
            stat = f.stat()
            agent_id = f.stem.replace("agent-", "")

            # Read first line to get agent metadata and task description
            slug = None
            task = None
            try:
                with open(f) as fp:
                    first_line = fp.readline()
                    if first_line:
                        data = json.loads(first_line)
                        slug = data.get("slug", "")
                        # Extract task from the prompt message
                        msg = data.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, str) and content:
                            # Get first line of prompt, truncated
                            task = content.split("\n")[0][:60]
            except (OSError, json.JSONDecodeError, KeyError):
                pass

            agents.append({
                "id": agent_id,
                "slug": slug,
                "task": task,
                "size": stat.st_size,
                "modified": int(stat.st_mtime * 1000),
                "path": str(f),
            })

        # Sort by modified time, newest first
        agents.sort(key=lambda x: x["modified"], reverse=True)
        return agents
    except Exception as e:
        _LOG.debug("Failed to list agents: %s", e)
        return []


def get_agent_events(agent_id: str, limit: int = 50) -> list[str]:
    """Get recent events from a specific subagent's log.

    Reads the last N lines from agent-{agent_id}.jsonl efficiently
    by reading backwards from the end.

    Args:
        agent_id: The agent ID (filename without agent- prefix and .jsonl).
        limit: Maximum number of events to return.

    Returns:
        List of JSON event strings, newest last.
    """
    subagents_dir = get_subagents_dir()
    if not subagents_dir:
        return []

    agent_file = subagents_dir / f"agent-{agent_id}.jsonl"
    if not agent_file.exists():
        return []

    try:
        # Read last N lines
        with open(agent_file, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            if file_size == 0:
                return []

            chunk_size = 8192
            lines: list[str] = []
            position = file_size

            while position > 0 and len(lines) <= limit:
                read_size = min(chunk_size, position)
                position -= read_size
                f.seek(position)
                chunk = f.read(read_size).decode("utf-8", errors="replace")
                chunk_lines = chunk.split("\n")
                if lines:
                    chunk_lines[-1] += lines[0]
                    lines = chunk_lines + lines[1:]
                else:
                    lines = chunk_lines

            return [line.strip() for line in lines[-limit:] if line.strip()]
    except Exception as e:
        _LOG.debug("Failed to read agent events: %s", e)
        return []


# =============================================================================
# Watcher Control
# =============================================================================


def start_watcher() -> None:
    """Start the file watcher background task.

    Creates an asyncio task running tail_stream() if not already running.
    Called on FastAPI startup when auth is configured.
    """
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        _watcher_task = asyncio.create_task(tail_stream())
        _LOG.info("Brain feed watcher started")
