"""Wendy Proxy API - Sandboxed endpoints for send_message, check_messages, and deploy.

This service acts as a proxy so Wendy (running in Claude CLI) can send messages,
check for new messages, and deploy sites without having direct access to the
Discord token or other sensitive environment variables.

Architecture:
    Wendy (Claude CLI) -> Proxy API (this service) -> Discord Bot / wendy-sites / wendy-games

Key Features:
    - Message sending via outbox file queue (bot picks up and sends)
    - Message checking with new-message interrupts (prevents stale replies)
    - Task completion notifications from the orchestrator
    - Site deployment to wendy.monster
    - Game deployment for multiplayer backends
    - Claude Code usage statistics

Endpoints:
    POST /api/send_message - Queue a message for sending to Discord
    GET  /api/check_messages/{channel_id} - Get new messages and task updates
    GET  /api/usage - Get Claude Code usage statistics
    POST /api/usage/refresh - Request immediate usage check
    POST /api/deploy_site - Deploy a static site to wendy.monster
    POST /api/deploy_game - Deploy a multiplayer game backend
    GET  /api/game_logs/{name} - Get logs from a running game server
    GET  /health - Health check endpoint
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

app = FastAPI(title="Wendy Proxy API")

# =============================================================================
# Configuration Constants
# =============================================================================

DB_PATH: str = os.getenv("WENDY_DB_PATH", "/data/wendy.db")
"""Path to the SQLite database containing cached Discord messages."""

OUTBOX_DIR: Path = Path("/data/wendy/outbox")
"""Directory where queued outgoing messages are written as JSON files."""

STATE_FILE: Path = Path("/data/wendy/message_check_state.json")
"""JSON file tracking last-seen message IDs per channel for interrupt detection."""

ATTACHMENTS_DIR: Path = Path("/data/wendy/attachments")
"""Directory where downloaded Discord attachments are stored."""

TASK_COMPLETIONS_FILE: Path = Path("/data/wendy/task_completions.json")
"""JSON file where orchestrator writes task completion notifications."""


# =============================================================================
# Request/Response Models
# =============================================================================


class SendMessageRequest(BaseModel):
    """Request body for sending a Discord message.

    Attributes:
        channel_id: Discord channel ID to send to.
        content: Message text content (max 2000 chars).
        message: Legacy alias for content (deprecated).
        attachment: Optional path to file to attach (must be in /data/wendy/ or /tmp/).
    """

    channel_id: str
    content: str | None = None
    message: str | None = None
    attachment: str | None = None


class SendMessageResponse(BaseModel):
    """Response from successful message send.

    Attributes:
        success: Always True for successful sends.
        message: Confirmation message with filename.
    """

    success: bool
    message: str


class NewMessagesError(BaseModel):
    """Returned when new messages arrived since last check (409 Conflict).

    Attributes:
        error: Human-readable error description.
        new_messages: List of messages that arrived.
        guidance: Instructions for the caller on how to handle this.
    """

    error: str
    new_messages: list
    guidance: str


class MessageInfo(BaseModel):
    """Information about a single Discord message.

    Attributes:
        message_id: Discord message snowflake ID.
        author: Display name of the message author.
        content: Message text content.
        timestamp: Unix timestamp (int) or ISO string.
        attachments: List of local file paths for any attachments.
    """

    message_id: int
    author: str
    content: str
    timestamp: int | str
    attachments: list[str] | None = None


class TaskUpdate(BaseModel):
    """Notification about a completed orchestrator task.

    Attributes:
        task_id: Beads task ID.
        title: Human-readable task title.
        status: Completion status ("completed" or "failed").
        duration: How long the task ran (e.g., "0:05:32").
        completed_at: ISO timestamp of completion.
    """

    task_id: str
    title: str
    status: str
    duration: str
    completed_at: str


class CheckMessagesResponse(BaseModel):
    """Response from check_messages endpoint.

    Attributes:
        messages: List of new messages since last check.
        task_updates: List of task completions to notify about.
    """

    messages: list[MessageInfo]
    task_updates: list[TaskUpdate]


# =============================================================================
# State Management Functions
# =============================================================================


def get_last_seen(channel_id: int) -> int | None:
    """Get the last seen message ID for a channel.

    Used for new-message interrupt detection. When Wendy calls check_messages,
    we record the newest message ID. If new messages arrive before Wendy sends
    her reply, we can detect this and reject the send.

    Args:
        channel_id: Discord channel ID.

    Returns:
        Last seen message ID, or None if no state exists for this channel.
    """
    if not STATE_FILE.exists():
        return None
    try:
        state = json.loads(STATE_FILE.read_text())
        return state.get("last_seen", {}).get(str(channel_id))
    except (OSError, json.JSONDecodeError):
        return None


def update_last_seen(channel_id: int, message_id: int) -> None:
    """Update the last seen message ID for a channel.

    Called after check_messages returns messages, and also after send_message
    detects new messages (so retries succeed).

    Args:
        channel_id: Discord channel ID.
        message_id: Newest message ID seen.
    """
    state: dict = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}

    if "last_seen" not in state:
        state["last_seen"] = {}

    state["last_seen"][str(channel_id)] = message_id
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# =============================================================================
# Attachment Handling
# =============================================================================


def find_attachments_for_message(message_id: int) -> list[str]:
    """Find local attachment files for a Discord message.

    The Discord bot saves attachments as msg_{message_id}_{index}_{filename}.
    This function finds all attachments associated with a message.

    Args:
        message_id: Discord message snowflake ID.

    Returns:
        Sorted list of absolute file paths for attachments.
    """
    if not ATTACHMENTS_DIR.exists():
        return []

    matching: list[str] = []
    for att_file in ATTACHMENTS_DIR.glob(f"msg_{message_id}_*"):
        matching.append(str(att_file))

    return sorted(matching)


# =============================================================================
# API Endpoints
# =============================================================================

DISCORD_MAX_MESSAGE_LENGTH: int = 2000
"""Maximum message length allowed by Discord."""


def check_for_new_messages(channel_id: int) -> list[dict]:
    """Check if new messages have arrived since last check_messages call.

    This is the core of the new-message interrupt system. It prevents Wendy
    from sending stale replies when users have sent additional messages.

    If new messages are found, the last_seen state is auto-updated so that
    a retry will succeed (after Wendy incorporates the new messages).

    Args:
        channel_id: Discord channel ID to check.

    Returns:
        List of new message dicts (with keys: message_id, author, content,
        timestamp), or empty list if no new messages.
    """
    last_seen = get_last_seen(channel_id)

    # If no last_seen, allow send (first message to this channel)
    if last_seen is None:
        return []

    db_path = Path(DB_PATH)
    if not db_path.exists():
        return []  # Fail open if DB unavailable

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        query = """
            SELECT message_id, author_name, content, timestamp
            FROM cached_messages
            WHERE channel_id = ? AND message_id > ?
            AND LOWER(author_name) NOT LIKE '%wendy%'
            AND content NOT LIKE '!%'
            AND content NOT LIKE '-%'
            ORDER BY message_id ASC
        """
        rows = conn.execute(query, (channel_id, last_seen)).fetchall()

        if rows:
            # Auto-update last_seen so retry will succeed
            newest_id = max(r["message_id"] for r in rows)
            update_last_seen(channel_id, newest_id)

            return [
                {
                    "message_id": row["message_id"],
                    "author": row["author_name"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                }
                for row in rows
            ]

        return []
    finally:
        conn.close()


@app.post("/api/send_message")
async def send_message(request: SendMessageRequest) -> dict:
    """Send a message to a Discord channel via the outbox queue.

    This endpoint writes a JSON file to the outbox directory, which the
    Discord bot picks up and sends to the channel.

    New-message interrupt handling:
        If new messages have arrived since the last check_messages call,
        returns the new messages instead of queuing. This prevents stale
        replies when users send additional messages while Wendy is responding.

    Args:
        request: SendMessageRequest with channel_id, content, and optional attachment.

    Returns:
        On success: {"success": True, "message": "Message queued: <filename>"}
        On interrupt: {"error": "...", "new_messages": [...], "guidance": "..."}

    Raises:
        HTTPException 400: Message too long or invalid attachment path.
        HTTPException 500: Server error.
    """
    try:
        channel_id = int(request.channel_id)

        # Check for new messages first
        new_messages = check_for_new_messages(channel_id)
        if new_messages:
            return {
                "error": "New messages received since your last check. Review them and retry.",
                "new_messages": new_messages,
                "guidance": (
                    "Prefer sending ONE message that responds to all users at once, "
                    "rather than one message per person. Edit your reply to address everyone, then retry. "
                    "IMPORTANT: Do NOT mention this to users. No references to 'catching duplicates', "
                    "'interrupts', 'warnings', or anything about internal systems. Just naturally respond to everyone."
                ),
            }

        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

        msg_text = request.content or request.message or ""

        # Validate message length - Discord has a 2000 char limit
        if len(msg_text) > DISCORD_MAX_MESSAGE_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Message too long ({len(msg_text)} chars). Discord limit is {DISCORD_MAX_MESSAGE_LENGTH}. "
                    f"For long content, write it to a file in /data/wendy/uploads/ and use the 'attachment' "
                    f"parameter to send it as a file attachment instead."
                )
            )

        # Validate attachment path if provided
        if request.attachment:
            att_path = Path(request.attachment)
            allowed_prefixes = ["/data/wendy/", "/tmp/"]
            if not any(request.attachment.startswith(p) for p in allowed_prefixes):
                raise HTTPException(
                    status_code=400,
                    detail=f"Attachment must be in /data/wendy/ or /tmp/, got: {request.attachment}"
                )
            if not att_path.exists():
                raise HTTPException(
                    status_code=400,
                    detail=f"Attachment file not found: {request.attachment}"
                )

        # Create outbox message
        timestamp_ns = time.time_ns()
        filename = f"{request.channel_id}_{timestamp_ns}.json"

        message_data = {
            "channel_id": request.channel_id,
            "message": msg_text,
        }
        if request.attachment:
            message_data["file_path"] = request.attachment

        outbox_path = OUTBOX_DIR / filename
        outbox_path.write_text(json.dumps(message_data))

        return {"success": True, "message": f"Message queued: {filename}"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/check_messages/{channel_id}")
async def check_messages(
    channel_id: int,
    limit: int = 10,
    all_messages: bool = False
) -> CheckMessagesResponse:
    """Check for new Discord messages and orchestrator task updates.

    This is Wendy's main endpoint for getting context. It returns recent
    messages in the channel and any completed tasks from the orchestrator.

    The last_seen state is updated after each call, which enables the
    new-message interrupt detection in send_message.

    Args:
        channel_id: Discord channel ID to check.
        limit: Maximum number of messages to return (default 10).
        all_messages: If True, return all messages regardless of last_seen.

    Returns:
        CheckMessagesResponse with messages (oldest first) and task_updates.
    """
    messages: list[MessageInfo] = []
    task_updates: list[TaskUpdate] = []

    # Get messages from database
    try:
        db_path = Path(DB_PATH)
        if db_path.exists():
            since_id = None if all_messages else get_last_seen(channel_id)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            try:
                if since_id:
                    query = """
                        SELECT message_id, channel_id, author_name, content, timestamp, has_images
                        FROM cached_messages
                        WHERE channel_id = ? AND message_id > ?
                        AND LOWER(author_name) NOT LIKE '%wendy%'
                        AND content NOT LIKE '!%'
                        AND content NOT LIKE '-%'
                        ORDER BY message_id DESC
                        LIMIT ?
                    """
                    rows = conn.execute(query, (channel_id, since_id, limit)).fetchall()
                else:
                    query = """
                        SELECT message_id, channel_id, author_name, content, timestamp, has_images
                        FROM cached_messages
                        WHERE channel_id = ?
                        AND LOWER(author_name) NOT LIKE '%wendy%'
                        AND content NOT LIKE '!%'
                        AND content NOT LIKE '-%'
                        ORDER BY message_id DESC
                        LIMIT ?
                    """
                    rows = conn.execute(query, (channel_id, limit)).fetchall()

                for row in rows:
                    attachments = find_attachments_for_message(row["message_id"])
                    msg = MessageInfo(
                        message_id=row["message_id"],
                        author=row["author_name"],
                        content=row["content"],
                        timestamp=row["timestamp"],
                        attachments=attachments if attachments else None,
                    )
                    messages.append(msg)

                # Return in chronological order (oldest first)
                messages = list(reversed(messages))

                # Update last_seen with the newest message_id
                if messages:
                    newest_id = max(m.message_id for m in messages)
                    update_last_seen(channel_id, newest_id)

            finally:
                conn.close()

    except Exception as e:
        # Log but don't fail - still return task updates
        print(f"Error reading messages: {e}")

    # Get task completions
    try:
        if TASK_COMPLETIONS_FILE.exists():
            completions = json.loads(TASK_COMPLETIONS_FILE.read_text())
            if not isinstance(completions, list):
                completions = completions.get("completions", [])

            # Find unseen completions
            unseen = [c for c in completions if not c.get("seen_by_proxy", False)]

            for c in unseen:
                task_updates.append(TaskUpdate(
                    task_id=c.get("task_id", "unknown"),
                    title=c.get("title", "Unknown task"),
                    status=c.get("status", "completed"),  # Read status string directly
                    duration=c.get("duration", "unknown"),
                    completed_at=c.get("completed_at", ""),
                ))

            # Mark as seen by proxy
            if unseen:
                for c in completions:
                    c["seen_by_proxy"] = True
                TASK_COMPLETIONS_FILE.write_text(json.dumps(completions, indent=2))

    except Exception as e:
        print(f"Error reading task completions: {e}")

    return CheckMessagesResponse(messages=messages, task_updates=task_updates)


@app.get("/health")
async def health() -> dict:
    """Health check endpoint for load balancers and monitoring."""
    return {"status": "ok"}


# =============================================================================
# Usage Statistics
# =============================================================================

USAGE_DATA_FILE: Path = Path("/data/wendy/usage_data.json")
"""JSON file where orchestrator writes latest Claude Code usage statistics."""

USAGE_FORCE_CHECK_FILE: Path = Path("/data/wendy/usage_force_check")
"""Sentinel file - touching this triggers immediate usage refresh."""


class UsageResponse(BaseModel):
    """Claude Code usage statistics response.

    Attributes:
        session_percent: Current session usage percentage.
        week_all_percent: Weekly usage (all models) percentage.
        week_sonnet_percent: Weekly usage (Sonnet only) percentage.
        timestamp: When the usage data was collected.
        updated_at: When the data was last written to disk.
        message: Human-readable formatted usage summary.
    """

    session_percent: int
    week_all_percent: int
    week_sonnet_percent: int
    timestamp: str
    updated_at: str
    message: str


@app.get("/api/usage", response_model=UsageResponse)
async def get_usage() -> UsageResponse:
    """Get current Claude Code usage statistics.

    Returns the latest usage data from the orchestrator's hourly polling.
    Use POST /api/usage/refresh to request an immediate update.

    Returns:
        UsageResponse with session and weekly usage percentages.

    Raises:
        HTTPException 404: Usage data not yet available.
        HTTPException 500: Error reading usage data.
    """
    if not USAGE_DATA_FILE.exists():
        raise HTTPException(
            status_code=404,
            detail="Usage data not available yet. The orchestrator polls hourly."
        )

    try:
        data = json.loads(USAGE_DATA_FILE.read_text())

        # Format a human-readable message
        week_all = data.get("week_all_percent", 0)
        week_sonnet = data.get("week_sonnet_percent", 0)
        updated = data.get("updated_at", "unknown")

        message = (
            f"Claude Code Usage (as of {updated}):\n"
            f"- Weekly (all models): {week_all}%\n"
            f"- Weekly (Sonnet only): {week_sonnet}%"
        )

        return UsageResponse(
            session_percent=data.get("session_percent", 0),
            week_all_percent=week_all,
            week_sonnet_percent=week_sonnet,
            timestamp=data.get("timestamp", ""),
            updated_at=updated,
            message=message
        )

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail="Failed to parse usage data") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/usage/refresh")
async def refresh_usage() -> dict:
    """Request an immediate usage check from the orchestrator.

    Creates a sentinel file that triggers the orchestrator to run a usage
    check on its next poll cycle (within 30 seconds typically).

    Returns:
        Success message with instructions to check back later.

    Raises:
        HTTPException 500: Error creating sentinel file.
    """
    try:
        USAGE_FORCE_CHECK_FILE.touch()
        return {"success": True, "message": "Usage refresh requested. Check back in ~30 seconds."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Site Deployment
# =============================================================================

WENDY_SITES_URL: str = os.getenv("WENDY_SITES_URL", "http://100.120.250.100:8910")
"""URL of the wendy-sites deployment service."""

WENDY_DEPLOY_TOKEN: str = os.getenv("WENDY_DEPLOY_TOKEN", "")
"""Authentication token for wendy-sites API."""

WENDY_GAMES_URL: str = os.getenv("WENDY_GAMES_URL", "http://100.120.250.100:8920")
"""URL of the wendy-games deployment service."""

WENDY_GAMES_TOKEN: str = os.getenv("WENDY_GAMES_TOKEN", "")
"""Authentication token for wendy-games API."""


class DeploySiteResponse(BaseModel):
    """Response from site deployment.

    Attributes:
        success: Whether deployment succeeded.
        url: Public URL of the deployed site (e.g., https://foo.wendy.monster).
        message: Human-readable status message.
    """

    success: bool
    url: str | None = None
    message: str


@app.post("/api/deploy_site", response_model=DeploySiteResponse)
async def deploy_site(
    name: str = Form(...),
    files: UploadFile = File(...),
) -> DeploySiteResponse:
    """Deploy a static site to wendy.monster.

    Accepts a tar.gz archive containing the site files and deploys them
    to https://{name}.wendy.monster via the wendy-sites service.

    Args:
        name: Site name (becomes subdomain: name.wendy.monster).
        files: Tar.gz archive containing site files (index.html, etc.).

    Returns:
        DeploySiteResponse with the public URL on success.

    Raises:
        HTTPException 500: WENDY_DEPLOY_TOKEN not configured.
        HTTPException 502: Cannot connect to wendy-sites service.
    """
    if not WENDY_DEPLOY_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="WENDY_DEPLOY_TOKEN not configured on server"
        )

    try:
        content = await files.read()

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{WENDY_SITES_URL}/api/deploy",
                data={"name": name},
                files={"files": ("site.tar.gz", content, "application/gzip")},
                headers={"Authorization": f"Bearer {WENDY_DEPLOY_TOKEN}"},
            )

        if response.status_code != 200:
            error_detail = response.text
            try:
                error_json = response.json()
                error_detail = error_json.get("detail", error_detail)
            except Exception:
                pass
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Deploy failed: {error_detail}"
            )

        result = response.json()
        return DeploySiteResponse(
            success=True,
            url=result.get("url"),
            message=result.get("message", "Site deployed successfully"),
        )

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to wendy-sites service: {str(e)}"
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Game Deployment
# =============================================================================


class DeployGameResponse(BaseModel):
    """Response from game deployment.

    Attributes:
        success: Whether deployment succeeded.
        url: Public HTTP URL for the game (if applicable).
        ws: WebSocket URL for real-time game connections.
        port: Assigned port number for the game server.
        message: Human-readable status message.
    """

    success: bool
    url: str | None = None
    ws: str | None = None
    port: int | None = None
    message: str


@app.get("/api/game_logs/{name}")
async def get_game_logs(name: str, lines: int = 100) -> dict:
    """Get recent logs from a running game server.

    Args:
        name: Game name/identifier.
        lines: Number of log lines to return (default 100).

    Returns:
        Dict with 'name' and 'logs' keys. Logs may be an error message
        if the game is not found or there's a connection error.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{WENDY_GAMES_URL}/api/games/{name}/logs",
                params={"lines": lines},
                headers={"Authorization": f"Bearer {WENDY_GAMES_TOKEN}"},
            )

        if response.status_code == 404:
            return {"name": name, "logs": f"Game '{name}' not found"}

        if response.status_code != 200:
            return {"name": name, "logs": f"Error: {response.text}"}

        return response.json()

    except httpx.RequestError as e:
        return {"name": name, "logs": f"Connection error: {str(e)}"}


@app.post("/api/deploy_game", response_model=DeployGameResponse)
async def deploy_game(
    name: str = Form(...),
    files: UploadFile = File(...),
) -> DeployGameResponse:
    """Deploy a multiplayer game backend to wendy.monster.

    Accepts a tar.gz archive containing the game server code and deploys it
    via the wendy-games service. Returns WebSocket connection details.

    Args:
        name: Game name/identifier (used for routing and logs).
        files: Tar.gz archive containing game server files.

    Returns:
        DeployGameResponse with WebSocket URL and port on success.

    Raises:
        HTTPException 500: WENDY_GAMES_TOKEN not configured.
        HTTPException 502: Cannot connect to wendy-games service.
    """
    if not WENDY_GAMES_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="WENDY_GAMES_TOKEN not configured on server"
        )

    try:
        content = await files.read()

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{WENDY_GAMES_URL}/api/deploy",
                data={"name": name},
                files={"files": ("game.tar.gz", content, "application/gzip")},
                headers={"Authorization": f"Bearer {WENDY_GAMES_TOKEN}"},
            )

        if response.status_code != 200:
            error_detail = response.text
            try:
                error_json = response.json()
                error_detail = error_json.get("detail", error_detail)
            except Exception:
                pass
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Deploy failed: {error_detail}"
            )

        result = response.json()
        return DeployGameResponse(
            success=True,
            url=result.get("url"),
            ws=result.get("ws"),
            port=result.get("port"),
            message=result.get("message", "Game deployed successfully"),
        )

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to wendy-games service: {str(e)}"
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
