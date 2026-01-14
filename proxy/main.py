"""Wendy Proxy API - Sandboxed endpoints for send_message, check_messages, and deploy.

This service acts as a proxy so Wendy (running in Claude CLI) can send messages,
check for new messages, and deploy sites without having direct access to the
Discord token or other sensitive environment variables.
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

app = FastAPI(title="Wendy Proxy API")

# Configuration
DB_PATH = os.getenv("WENDY_DB_PATH", "/data/wendy.db")
OUTBOX_DIR = Path("/data/wendy/outbox")
STATE_FILE = Path("/data/wendy/message_check_state.json")
ATTACHMENTS_DIR = Path("/data/wendy/attachments")
TASK_COMPLETIONS_FILE = Path("/data/wendy/task_completions.json")


class SendMessageRequest(BaseModel):
    channel_id: str
    content: Optional[str] = None
    message: Optional[str] = None  # Legacy field name
    attachment: Optional[str] = None


class SendMessageResponse(BaseModel):
    success: bool
    message: str


class NewMessagesError(BaseModel):
    error: str
    new_messages: list
    guidance: str


class MessageInfo(BaseModel):
    message_id: int
    author: str
    content: str
    timestamp: int | str
    attachments: Optional[list[str]] = None


class TaskUpdate(BaseModel):
    task_id: str
    title: str
    status: str  # "completed" or "failed"
    duration: str
    completed_at: str


class CheckMessagesResponse(BaseModel):
    messages: list[MessageInfo]
    task_updates: list[TaskUpdate]


# ==================== State Management ====================

def get_last_seen(channel_id: int) -> Optional[int]:
    """Get the last seen message_id for a channel."""
    if not STATE_FILE.exists():
        return None
    try:
        state = json.loads(STATE_FILE.read_text())
        return state.get("last_seen", {}).get(str(channel_id))
    except (json.JSONDecodeError, IOError):
        return None


def update_last_seen(channel_id: int, message_id: int) -> None:
    """Update the last seen message_id for a channel."""
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            state = {}

    if "last_seen" not in state:
        state["last_seen"] = {}

    state["last_seen"][str(channel_id)] = message_id
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ==================== Attachments ====================

def find_attachments_for_message(message_id: int) -> list[str]:
    """Find attachment files for a message ID."""
    if not ATTACHMENTS_DIR.exists():
        return []

    matching = []
    for att_file in ATTACHMENTS_DIR.glob(f"msg_{message_id}_*"):
        matching.append(str(att_file))

    return sorted(matching)


# ==================== Endpoints ====================

DISCORD_MAX_MESSAGE_LENGTH = 2000


def check_for_new_messages(channel_id: int) -> list[dict]:
    """Check if there are new messages since last check_messages call.

    Returns list of new messages if any exist, empty list otherwise.
    Also auto-updates last_seen so retry will succeed.
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
async def send_message(request: SendMessageRequest):
    """Send a message to a Discord channel via the outbox.

    If new messages have arrived since the last check_messages call,
    returns a 409 Conflict with those messages. The caller should
    review the messages, update their reply, and retry.
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
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/check_messages/{channel_id}")
async def check_messages(
    channel_id: int,
    limit: int = 10,
    all_messages: bool = False
) -> CheckMessagesResponse:
    """Check for new messages and task updates in a channel."""
    messages = []
    task_updates = []

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
                    status="completed" if c.get("success", False) else "failed",
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
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


# ==================== Site Deployment ====================

WENDY_SITES_URL = os.getenv("WENDY_SITES_URL", "http://100.120.250.100:8910")
WENDY_DEPLOY_TOKEN = os.getenv("WENDY_DEPLOY_TOKEN", "")
WENDY_GAMES_URL = os.getenv("WENDY_GAMES_URL", "http://100.120.250.100:8920")
WENDY_GAMES_TOKEN = os.getenv("WENDY_GAMES_TOKEN", "")


class DeploySiteResponse(BaseModel):
    success: bool
    url: Optional[str] = None
    message: str


@app.post("/api/deploy_site", response_model=DeploySiteResponse)
async def deploy_site(
    name: str = Form(...),
    files: UploadFile = File(...),
):
    """Deploy a site to wendy.monster."""
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
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Game Deployment ====================

class DeployGameResponse(BaseModel):
    success: bool
    url: Optional[str] = None
    ws: Optional[str] = None
    port: Optional[int] = None
    message: str


@app.get("/api/game_logs/{name}")
async def get_game_logs(name: str, lines: int = 100):
    """Get logs from a game server."""
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
):
    """Deploy a multiplayer game backend to wendy.monster."""
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
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
