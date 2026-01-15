"""Brain feed - real-time stream of Wendy's Claude Code session."""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Set

from fastapi import WebSocket
from watchfiles import awatch, Change

_LOG = logging.getLogger(__name__)

STREAM_FILE = Path("/data/wendy/stream.jsonl")
SESSION_STATE_FILE = Path("/data/wendy/session_state.json")
DB_PATH = Path("/data/wendy/wendy.db")
CLAUDE_DIR = Path("/data/claude")
MAX_HISTORY = 50
MAX_CLIENTS = 100
CONTEXT_WINDOW = 200000  # Claude's context window

connected_clients: Set[WebSocket] = set()
_watcher_task: asyncio.Task | None = None

# Track latest stats from stream events
_latest_stats: dict = {
    "context_tokens": 0,
    "context_pct": 0,
    "session_cost": 0.0,
    "last_activity": None,
    "active_tasks": 0,
}


def get_recent_events(n: int = MAX_HISTORY) -> list[str]:
    """Get last N events from stream file efficiently."""
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


async def broadcast(message: str) -> None:
    """Send message to all connected clients."""
    if not connected_clients:
        return

    dead: Set[WebSocket] = set()
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
    """Watch stream.jsonl and broadcast new events."""
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
                            # Jump to END of current file to skip the rewritten content
                            pos = current_size
                            # Wait a bit for the trim operation to complete
                            await asyncio.sleep(0.5)
                            # Now jump to whatever the new size is
                            try:
                                pos = STREAM_FILE.stat().st_size
                                _LOG.info("After truncation settle, pos now %d", pos)
                            except:
                                pass
                            continue

                        if current_size <= pos:
                            continue

                        with open(STREAM_FILE, "r") as f:
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


async def add_client(ws: WebSocket) -> bool:
    """Add a client connection. Returns False if at capacity."""
    if len(connected_clients) >= MAX_CLIENTS:
        return False
    connected_clients.add(ws)
    _LOG.info("Client connected, total: %d", len(connected_clients))
    return True


def remove_client(ws: WebSocket) -> None:
    """Remove a client connection."""
    connected_clients.discard(ws)
    _LOG.info("Client disconnected, total: %d", len(connected_clients))


def client_count() -> int:
    """Get number of connected clients."""
    return len(connected_clients)


def get_stats() -> dict:
    """Get current brain stats."""
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

    # Get session state
    try:
        if SESSION_STATE_FILE.exists():
            state = json.loads(SESSION_STATE_FILE.read_text())
            # Get first (and usually only) channel's stats
            for channel_id, session in state.items():
                stats["session_messages"] = session.get("message_count", 0)
                stats["session_id"] = session.get("session_id", "")[:8]
                stats["total_input"] = session.get("total_input_tokens", 0)
                stats["total_output"] = session.get("total_output_tokens", 0)
                stats["cache_read"] = session.get("total_cache_read_tokens", 0)
                break
    except Exception as e:
        _LOG.debug("Failed to read session state: %s", e)

    # Get cached messages count
    try:
        if DB_PATH.exists():
            conn = sqlite3.connect(DB_PATH)
            count = conn.execute("SELECT COUNT(*) FROM cached_messages").fetchone()[0]
            conn.close()
            stats["cached_messages"] = count
    except Exception as e:
        _LOG.debug("Failed to count cached messages: %s", e)

    return stats


def update_stats_from_event(event_json: str) -> None:
    """Update stats from a stream event."""
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

        # Track active Task tool calls
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_use" and block.get("name") == "Task":
                    _latest_stats["active_tasks"] += 1

        # Decrement tasks on tool results (rough tracking)
        if event.get("type") == "user":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_result":
                    if _latest_stats["active_tasks"] > 0:
                        _latest_stats["active_tasks"] -= 1

    except Exception as e:
        _LOG.debug("Failed to parse event for stats: %s", e)


def get_subagents_dir() -> Path | None:
    """Get the subagents directory for the current session."""
    try:
        if not SESSION_STATE_FILE.exists():
            return None
        state = json.loads(SESSION_STATE_FILE.read_text())
        for channel_id, session in state.items():
            session_id = session.get("session_id")
            if session_id:
                subagents_dir = CLAUDE_DIR / "projects" / "-data-wendy" / session_id / "subagents"
                if subagents_dir.exists():
                    return subagents_dir
        return None
    except Exception as e:
        _LOG.debug("Failed to get subagents dir: %s", e)
        return None


def list_agents() -> list[dict]:
    """List all agent files with metadata."""
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
                with open(f, "r") as fp:
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
            except:
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
    """Get recent events from a specific agent."""
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


def start_watcher() -> None:
    """Start the file watcher background task."""
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        _watcher_task = asyncio.create_task(tail_stream())
        _LOG.info("Brain feed watcher started")
