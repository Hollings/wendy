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
    - Multimodal file analysis via Gemini API

Endpoints:
    POST /api/send_message - Queue a message for sending to Discord
    GET  /api/check_messages/{channel_id} - Get new messages and task updates
    GET  /api/usage - Get Claude Code usage statistics
    POST /api/usage/refresh - Request immediate usage check
    POST /api/deploy_site - Deploy a static site to wendy.monster
    POST /api/deploy_game - Deploy a multiplayer game backend
    GET  /api/game_logs/{name} - Get logs from a running game server
    POST /api/analyze_file - Analyze image/audio/video files using Gemini
    GET  /health - Health check endpoint
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

# Add bot module to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from bot.paths import (
    DB_PATH,
    OUTBOX_DIR,
    SHARED_DIR,
    WENDY_BASE,
    attachments_dir,
    ensure_shared_dirs,
)
from bot.state_manager import state as state_manager

app = FastAPI(title="Wendy Proxy API")

# Ensure shared directories exist at startup
ensure_shared_dirs()

# =============================================================================
# Channel Config (for channel_id -> channel_name mapping)
# =============================================================================

_CHANNEL_CONFIG: dict[int, dict] = {}
"""Map of channel_id -> channel config (parsed from WENDY_CHANNEL_CONFIG)."""


def _load_channel_config() -> None:
    """Load channel config from WENDY_CHANNEL_CONFIG env var."""
    global _CHANNEL_CONFIG
    config_str = os.getenv("WENDY_CHANNEL_CONFIG", "")
    if not config_str:
        return

    try:
        configs = json.loads(config_str)
        for cfg in configs:
            channel_id = int(cfg.get("id", 0))
            if channel_id:
                _CHANNEL_CONFIG[channel_id] = cfg
    except (json.JSONDecodeError, ValueError, TypeError):
        pass


def get_channel_name(channel_id: int) -> str | None:
    """Get channel name for a channel ID from config.

    Returns the channel's folder name (_folder key takes precedence over name).
    """
    cfg = _CHANNEL_CONFIG.get(channel_id)
    if not cfg:
        return None
    return cfg.get("_folder") or cfg.get("name")


# Load config at startup
_load_channel_config()


# =============================================================================
# Request/Response Models
# =============================================================================


class ActionItem(BaseModel):
    """A single action within a batch send_message request.

    Attributes:
        type: Action type - "send_message" or "add_reaction".
        content: Message text (for send_message).
        file_path: Path to file attachment (for send_message).
        attachment: Alias for file_path (for send_message).
        reply_to: Message ID to reply to (for send_message).
        message_id: Target message ID (for add_reaction).
        emoji: Emoji name or custom emoji string (for add_reaction).
    """

    type: str
    content: str | None = None
    file_path: str | None = None
    attachment: str | None = None
    reply_to: int | None = None
    message_id: int | None = None
    emoji: str | None = None


class SendMessageRequest(BaseModel):
    """Request body for sending a Discord message.

    Attributes:
        channel_id: Discord channel ID to send to.
        content: Message text content (max 2000 chars).
        message: Legacy alias for content (deprecated).
        attachment: Optional path to file to attach (must be in /data/wendy/ or /tmp/).
        reply_to: Optional message ID to reply to.
        actions: Optional list of batch actions (overrides content/message/attachment).
    """

    channel_id: str
    content: str | None = None
    message: str | None = None
    attachment: str | None = None
    reply_to: int | None = None
    actions: list[ActionItem] | None = None


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


class ReplyContext(BaseModel):
    """Context for a message that is a reply to another message.

    Attributes:
        message_id: Discord message ID of the original message.
        author: Display name of the original message author.
        content: Text content of the original message.
    """

    message_id: int
    author: str
    content: str


class MessageInfo(BaseModel):
    """Information about a single Discord message.

    Attributes:
        message_id: Discord message snowflake ID.
        author: Display name of the message author.
        content: Message text content.
        timestamp: Unix timestamp (int) or ISO string.
        attachments: List of local file paths for any attachments.
        reply_to: Context of the message being replied to, if this is a reply.
    """

    message_id: int
    author: str
    content: str
    timestamp: int | str
    attachments: list[str] | None = None
    reply_to: ReplyContext | None = None


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


class AnalyzeFileResponse(BaseModel):
    """Response from Gemini file analysis endpoint.

    Attributes:
        success: Whether analysis succeeded.
        analysis: The AI-generated analysis text.
        media_type: MIME type of the analyzed file.
        model: Gemini model used for analysis.
    """

    success: bool
    analysis: str
    media_type: str
    model: str


# =============================================================================
# Gemini API Configuration
# =============================================================================

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
"""API key for Google Gemini API."""

GEMINI_MAX_FILE_SIZE: int = 20 * 1024 * 1024  # 20MB
"""Maximum file size for inline base64 uploads to Gemini."""

GEMINI_MAX_VIDEO_DURATION: int = 5 * 60  # 5 minutes
"""Maximum video duration in seconds."""

GEMINI_MAX_AUDIO_DURATION: int = 30 * 60  # 30 minutes
"""Maximum audio duration in seconds."""

SUPPORTED_IMAGE_TYPES: set[str] = {
    "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif",
}
"""Image MIME types supported by Gemini."""

SUPPORTED_AUDIO_TYPES: set[str] = {
    "audio/wav", "audio/mp3", "audio/mpeg", "audio/aiff", "audio/aac",
    "audio/ogg", "audio/flac",
}
"""Audio MIME types supported by Gemini."""

SUPPORTED_VIDEO_TYPES: set[str] = {
    "video/mp4", "video/mpeg", "video/quicktime", "video/avi",
    "video/x-flv", "video/webm", "video/x-ms-wmv", "video/3gpp",
}
"""Video MIME types supported by Gemini."""

SUPPORTED_MEDIA_TYPES: set[str] = SUPPORTED_IMAGE_TYPES | SUPPORTED_AUDIO_TYPES | SUPPORTED_VIDEO_TYPES
"""All media types supported for Gemini analysis."""

# File extension to MIME type mapping for fallback detection
EXTENSION_TO_MIME: dict[str, str] = {
    # Images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    # Audio
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".aiff": "audio/aiff",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    # Video
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".mov": "video/quicktime",
    ".avi": "video/avi",
    ".flv": "video/x-flv",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
    ".3gp": "video/3gpp",
    ".3gpp": "video/3gpp",
}
"""Mapping from file extensions to MIME types for fallback detection."""


def get_media_duration(content: bytes, media_type: str) -> float | None:
    """Get duration of audio/video content using ffprobe.

    Args:
        content: Raw file bytes.
        media_type: MIME type of the file.

    Returns:
        Duration in seconds, or None if unable to determine.
    """
    # Only check duration for audio/video
    if media_type not in SUPPORTED_AUDIO_TYPES and media_type not in SUPPORTED_VIDEO_TYPES:
        return None

    try:
        # Write to temp file for ffprobe
        suffix = ".mp4" if media_type.startswith("video/") else ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet", "-show_entries",
                    "format=duration", "-of", "csv=p=0", temp_path
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        finally:
            Path(temp_path).unlink(missing_ok=True)

    except Exception:
        pass

    return None


def infer_media_type(filename: str | None, content_type: str | None) -> str:
    """Infer media type from content_type header or filename extension.

    Args:
        filename: Original filename (may be None).
        content_type: Content-Type header from upload (may be None or generic).

    Returns:
        Best guess at MIME type, or empty string if unknown.
    """
    # First try the content_type if it's specific
    if content_type and content_type != "application/octet-stream":
        return content_type

    # Fall back to filename extension
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in EXTENSION_TO_MIME:
            return EXTENSION_TO_MIME[ext]

    return content_type or ""


def get_gemini_model(media_type: str) -> str:
    """Select the appropriate Gemini model based on media type.

    Args:
        media_type: MIME type of the file.

    Returns:
        Gemini model identifier.
    """
    if media_type in SUPPORTED_VIDEO_TYPES:
        return "gemini-2.5-pro"
    return "gemini-3-pro-preview"


def get_video_resolution(duration: float | None) -> str:
    """Select video resolution based on duration to manage token usage.

    Args:
        duration: Video duration in seconds, or None if unknown.

    Returns:
        Gemini media resolution setting.
    """
    if duration is None:
        return "MEDIA_RESOLUTION_MEDIUM"  # Safe default
    if duration <= 30:
        return "MEDIA_RESOLUTION_HIGH"
    if duration <= 120:
        return "MEDIA_RESOLUTION_MEDIUM"
    return "MEDIA_RESOLUTION_LOW"


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
    return state_manager.get_last_seen(channel_id)


def update_last_seen(channel_id: int, message_id: int) -> None:
    """Update the last seen message ID for a channel.

    Called after check_messages returns messages, and also after send_message
    detects new messages (so retries succeed).

    Args:
        channel_id: Discord channel ID.
        message_id: Newest message ID seen.
    """
    state_manager.update_last_seen(channel_id, message_id)


# =============================================================================
# Attachment Handling
# =============================================================================


def find_attachments_for_message(message_id: int, channel_name: str | None = None) -> list[str]:
    """Find local attachment files for a Discord message.

    The Discord bot saves attachments as msg_{message_id}_{index}_{filename}.
    This function finds all attachments associated with a message.

    Attachments are stored per-channel to ensure isolation between channels.
    If channel_name is not provided, no attachments will be found.

    Args:
        message_id: Discord message snowflake ID.
        channel_name: Channel name (folder name) where attachments are stored.

    Returns:
        Sorted list of absolute file paths for attachments.
    """
    if not channel_name:
        return []

    att_dir = attachments_dir(channel_name)
    if not att_dir.exists():
        return []

    matching: list[str] = []
    for att_file in att_dir.glob(f"msg_{message_id}_*"):
        matching.append(str(att_file))

    return sorted(matching)


# =============================================================================
# API Endpoints
# =============================================================================

DISCORD_MAX_MESSAGE_LENGTH: int = 2000
"""Maximum message length allowed by Discord."""

WENDY_BOT_ID: int = 771821437199581204
"""Wendy's Discord bot user ID, used to filter out her own messages."""

SYNTHETIC_ID_THRESHOLD: int = 9_000_000_000_000_000_000
"""Message IDs at or above this are synthetic one-time notifications."""

MAX_MESSAGE_LIMIT: int = 200
"""Upper bound for limit/count parameters on check_messages."""


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

    if not DB_PATH.exists():
        return []  # Fail open if DB unavailable

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    try:
        query = """
            SELECT m.message_id, m.author_nickname, m.content, m.timestamp,
                   m.reply_to_id,
                   r.author_nickname as reply_author,
                   r.content as reply_content
            FROM message_history m
            LEFT JOIN message_history r ON m.reply_to_id = r.message_id
            WHERE m.channel_id = ? AND m.message_id > ?
            AND m.author_id != ?
            AND m.content NOT LIKE '!%'
            AND m.content NOT LIKE '-%'
            ORDER BY m.message_id ASC
        """
        rows = conn.execute(query, (channel_id, last_seen, WENDY_BOT_ID)).fetchall()

        # Filter out synthetic messages - they're one-time
        # notifications that shouldn't trigger the "new message interrupt"
        real_rows = [r for r in rows if r["message_id"] < SYNTHETIC_ID_THRESHOLD]

        if real_rows:
            # Update last_seen with newest real message
            newest_id = max(r["message_id"] for r in real_rows)
            update_last_seen(channel_id, newest_id)

            result = []
            for row in real_rows:
                msg = {
                    "message_id": row["message_id"],
                    "author": row["author_nickname"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                }
                if row["reply_to_id"] and row["reply_author"]:
                    msg["reply_to"] = {
                        "message_id": row["reply_to_id"],
                        "author": row["reply_author"],
                        "content": row["reply_content"] or "",
                    }
                result.append(msg)
            return result

        return []
    finally:
        conn.close()


def _validate_attachment_path(path_str: str) -> None:
    """Validate that an attachment path is within allowed directories.

    Args:
        path_str: Filesystem path to validate.

    Raises:
        HTTPException 400: Path outside allowed directories or file not found.
    """
    att_path = Path(path_str).resolve()
    allowed_parents = [WENDY_BASE.resolve(), Path("/tmp").resolve()]
    if not any(att_path == parent or parent in att_path.parents for parent in allowed_parents):
        raise HTTPException(
            status_code=400,
            detail=f"Attachment must be in {WENDY_BASE}/ or /tmp/, got: {path_str}"
        )
    if not att_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Attachment file not found: {path_str}"
        )


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

        # Batch actions mode
        if request.actions:
            for i, action in enumerate(request.actions):
                if action.type == "send_message":
                    text = action.content or ""
                    if len(text) > DISCORD_MAX_MESSAGE_LENGTH:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Action {i}: message too long ({len(text)} chars). Discord limit is {DISCORD_MAX_MESSAGE_LENGTH}."
                        )
                    att = action.file_path or action.attachment
                    if att:
                        _validate_attachment_path(att)
                elif action.type == "add_reaction":
                    if not action.message_id:
                        raise HTTPException(status_code=400, detail=f"Action {i}: add_reaction requires message_id")
                    if not action.emoji:
                        raise HTTPException(status_code=400, detail=f"Action {i}: add_reaction requires emoji")
                else:
                    raise HTTPException(status_code=400, detail=f"Action {i}: unknown type '{action.type}'")

            timestamp_ns = time.time_ns()
            filename = f"{request.channel_id}_{timestamp_ns}.json"
            message_data = {
                "channel_id": request.channel_id,
                "actions": [a.model_dump(exclude_none=True) for a in request.actions],
            }
            outbox_path = OUTBOX_DIR / filename
            tmp_path = OUTBOX_DIR / f".{filename}.tmp"
            tmp_path.write_text(json.dumps(message_data))
            tmp_path.rename(outbox_path)
            return {"success": True, "message": f"Batch queued ({len(request.actions)} actions): {filename}"}

        # Single message mode
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
            _validate_attachment_path(request.attachment)

        # Create outbox message
        timestamp_ns = time.time_ns()
        filename = f"{request.channel_id}_{timestamp_ns}.json"

        message_data = {
            "channel_id": request.channel_id,
            "message": msg_text,
        }
        if request.attachment:
            message_data["file_path"] = request.attachment
        if request.reply_to:
            message_data["reply_to"] = request.reply_to

        outbox_path = OUTBOX_DIR / filename
        tmp_path = OUTBOX_DIR / f".{filename}.tmp"
        tmp_path.write_text(json.dumps(message_data))
        tmp_path.rename(outbox_path)

        return {"success": True, "message": f"Message queued: {filename}"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in send_message: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@app.get("/api/check_messages/{channel_id}")
async def check_messages(
    channel_id: int,
    limit: int = 10,
    all_messages: bool = False,
    count: int | None = None,
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
        count: If provided, fetch exactly this many messages regardless of
            last_seen state. Use count=20 after session continuation to
            restore conversation context.

    Returns:
        CheckMessagesResponse with messages (oldest first) and task_updates.
    """
    # Cap limit and count to prevent unbounded queries
    limit = min(limit, MAX_MESSAGE_LIMIT)
    if count is not None:
        count = min(count, MAX_MESSAGE_LIMIT)

    # Look up channel name from config for finding attachments
    channel_name = get_channel_name(channel_id)
    messages: list[MessageInfo] = []
    task_updates: list[TaskUpdate] = []

    # Get messages from database
    try:
        if DB_PATH.exists():
            # If count is specified, ignore last_seen and fetch exactly N messages
            if count is not None:
                since_id = None
                limit = count
            else:
                since_id = None if all_messages else get_last_seen(channel_id)

            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row

            try:
                if since_id:
                    query = """
                        SELECT m.message_id, m.channel_id, m.author_nickname, m.content, m.timestamp,
                               CASE WHEN m.attachment_urls IS NOT NULL THEN 1 ELSE 0 END as has_images,
                               m.reply_to_id,
                               r.author_nickname as reply_author,
                               r.content as reply_content
                        FROM message_history m
                        LEFT JOIN message_history r ON m.reply_to_id = r.message_id
                        WHERE m.channel_id = ? AND m.message_id > ?
                        AND m.author_id != ?
                        AND m.content NOT LIKE '!%'
                        AND m.content NOT LIKE '-%'
                        ORDER BY m.message_id DESC
                        LIMIT ?
                    """
                    rows = conn.execute(query, (channel_id, since_id, WENDY_BOT_ID, limit)).fetchall()
                else:
                    query = """
                        SELECT m.message_id, m.channel_id, m.author_nickname, m.content, m.timestamp,
                               CASE WHEN m.attachment_urls IS NOT NULL THEN 1 ELSE 0 END as has_images,
                               m.reply_to_id,
                               r.author_nickname as reply_author,
                               r.content as reply_content
                        FROM message_history m
                        LEFT JOIN message_history r ON m.reply_to_id = r.message_id
                        WHERE m.channel_id = ?
                        AND m.author_id != ?
                        AND m.content NOT LIKE '!%'
                        AND m.content NOT LIKE '-%'
                        ORDER BY m.message_id DESC
                        LIMIT ?
                    """
                    rows = conn.execute(query, (channel_id, WENDY_BOT_ID, limit)).fetchall()

                for row in rows:
                    attachments = find_attachments_for_message(row["message_id"], channel_name)

                    # Build reply context if this message is a reply
                    reply_to = None
                    if row["reply_to_id"] and row["reply_author"]:
                        reply_to = ReplyContext(
                            message_id=row["reply_to_id"],
                            author=row["reply_author"],
                            content=row["reply_content"] or "",
                        )

                    msg = MessageInfo(
                        message_id=row["message_id"],
                        author=row["author_nickname"],
                        content=row["content"],
                        timestamp=row["timestamp"],
                        attachments=attachments if attachments else None,
                        reply_to=reply_to,
                    )
                    messages.append(msg)

                # Return in chronological order (oldest first)
                messages = list(reversed(messages))

                # Separate synthetic messages from real messages
                # Synthetic messages are one-time notifications (webhooks, etc.) that
                # should be shown to Claude once then deleted
                synthetic_ids = [m.message_id for m in messages if m.message_id >= SYNTHETIC_ID_THRESHOLD]
                real_messages = [m for m in messages if m.message_id < SYNTHETIC_ID_THRESHOLD]

                # Update last_seen with newest REAL message ID only
                if real_messages:
                    newest_id = max(m.message_id for m in real_messages)
                    update_last_seen(channel_id, newest_id)

                # Delete synthetic messages after they've been read (they're one-time)
                if synthetic_ids:
                    placeholders = ",".join("?" * len(synthetic_ids))
                    conn.execute(
                        f"DELETE FROM message_history WHERE message_id IN ({placeholders})",
                        synthetic_ids
                    )
                    conn.commit()

            finally:
                conn.close()

    except Exception as e:
        # Log but don't fail - still return task updates
        print(f"Error reading messages: {e}")

    # Get task completion notifications from SQLite
    try:
        unseen_notifications = state_manager.get_unseen_notifications_for_proxy()

        # Filter for task_completion type and build TaskUpdate objects
        notification_ids = []
        for n in unseen_notifications:
            notification_ids.append(n.id)

            if n.type == "task_completion" and n.payload:
                task_updates.append(TaskUpdate(
                    task_id=n.payload.get("task_id", "unknown"),
                    title=n.title,
                    status=n.payload.get("status", "completed"),
                    duration=n.payload.get("duration", "unknown"),
                    completed_at=n.created_at,
                ))

        # Mark all as seen by proxy
        if notification_ids:
            state_manager.mark_notifications_seen_by_proxy(notification_ids)

    except Exception as e:
        print(f"Error reading notifications: {e}")

    return CheckMessagesResponse(messages=messages, task_updates=task_updates)


@app.get("/health")
async def health() -> dict:
    """Health check endpoint for load balancers and monitoring."""
    return {"status": "ok"}


EMOJI_CACHE_FILE: Path = SHARED_DIR / "emojis.json"
"""JSON file where the bot caches guild custom emojis."""


@app.get("/api/emojis")
async def get_emojis(search: str | None = None) -> dict:
    """Get available custom server emojis.

    Args:
        search: Optional search term to filter emoji names.

    Returns:
        Dict with 'custom' key containing list of emoji objects.
    """
    if not EMOJI_CACHE_FILE.exists():
        return {"custom": []}

    try:
        emojis = json.loads(EMOJI_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"custom": []}

    if search:
        term = search.lower()
        emojis = [e for e in emojis if term in e.get("name", "").lower()]

    return {"custom": emojis}


# =============================================================================
# Usage Statistics
# =============================================================================

USAGE_DATA_FILE: Path = WENDY_BASE / "usage_data.json"
"""JSON file where orchestrator writes latest Claude Code usage statistics."""

USAGE_FORCE_CHECK_FILE: Path = WENDY_BASE / "usage_force_check"
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
        print(f"Error in get_usage: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


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
        print(f"Error in refresh_usage: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


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
        print(f"Error in deploy_site: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


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
        print(f"Error in deploy_game: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


# =============================================================================
# File Analysis (Gemini)
# =============================================================================


@app.post("/api/analyze_file", response_model=AnalyzeFileResponse)
async def analyze_file(
    file: UploadFile = File(...),
    prompt: str = Form(...),
) -> AnalyzeFileResponse:
    """Analyze an image, audio, or video file using Google Gemini.

    This endpoint accepts media files and uses Gemini's multimodal capabilities
    to analyze them based on the provided prompt.

    Args:
        file: Media file to analyze (image, audio, or video).
        prompt: Analysis prompt (e.g., "What is in this image?").

    Returns:
        AnalyzeFileResponse with the AI-generated analysis.

    Raises:
        HTTPException 400: Unsupported file type or file too large.
        HTTPException 500: GEMINI_API_KEY not configured.
        HTTPException 502: Gemini API error.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY not configured on server"
        )

    # Determine media type from content_type header or filename
    media_type = infer_media_type(file.filename, file.content_type)
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type: {media_type}. "
                f"Supported: images (PNG, JPEG, WEBP, HEIC), "
                f"audio (WAV, MP3, AAC, OGG, FLAC), "
                f"video (MP4, MPEG, MOV, AVI, WEBM, WMV)"
            )
        )

    # Read file content
    content = await file.read()

    # Check file size
    if len(content) > GEMINI_MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({len(content) / 1024 / 1024:.1f}MB). Maximum size is 20MB."
        )

    # Check duration for audio/video files
    duration: float | None = None
    if media_type in SUPPORTED_VIDEO_TYPES or media_type in SUPPORTED_AUDIO_TYPES:
        duration = get_media_duration(content, media_type)

        if duration is not None:
            if media_type in SUPPORTED_VIDEO_TYPES and duration > GEMINI_MAX_VIDEO_DURATION:
                raise HTTPException(
                    status_code=400,
                    detail=f"Video too long ({duration / 60:.1f} min). Maximum is 5 minutes."
                )
            if media_type in SUPPORTED_AUDIO_TYPES and duration > GEMINI_MAX_AUDIO_DURATION:
                raise HTTPException(
                    status_code=400,
                    detail=f"Audio too long ({duration / 60:.1f} min). Maximum is 30 minutes."
                )

    # Select model based on media type
    model = get_gemini_model(media_type)

    # Encode file as base64
    file_base64 = base64.standard_b64encode(content).decode("utf-8")

    # Build Gemini API request
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    request_body: dict = {
        "contents": [{
            "parts": [
                {
                    "inline_data": {
                        "mime_type": media_type,
                        "data": file_base64,
                    }
                },
                {"text": prompt},
            ]
        }]
    }

    # Set video resolution based on duration to manage token usage
    # <30s = HIGH, 30s-2min = MEDIUM, >2min = LOW
    if media_type in SUPPORTED_VIDEO_TYPES:
        resolution = get_video_resolution(duration)
        request_body["generation_config"] = {"media_resolution": resolution}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                gemini_url,
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json=request_body,
            )

        if response.status_code != 200:
            error_detail = response.text
            try:
                error_json = response.json()
                if "error" in error_json:
                    error_detail = error_json["error"].get("message", error_detail)
            except Exception:
                pass
            raise HTTPException(
                status_code=502,
                detail=f"Gemini API error: {error_detail}"
            )

        result = response.json()

        # Extract text from response
        try:
            analysis = result["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise HTTPException(
                status_code=502,
                detail=f"Unexpected Gemini response format: {result}"
            ) from e

        return AnalyzeFileResponse(
            success=True,
            analysis=analysis,
            media_type=media_type,
            model=model,
        )

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to Gemini API: {str(e)}"
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in analyze_file: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
