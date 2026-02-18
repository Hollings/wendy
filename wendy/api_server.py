"""Internal HTTP server (aiohttp) that Claude CLI curls.

Replaces the v1 proxy service. Runs in-process, calls discord.py directly.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from . import config as _config
from .config import (
    DISCORD_MAX_MESSAGE_LENGTH,
    MAX_MESSAGE_LIMIT,
    SYNTHETIC_ID_THRESHOLD,
)
from .paths import SHARED_DIR, WENDY_BASE, attachments_dir
from .state import state as state_manager

if TYPE_CHECKING:
    import discord

_LOG = logging.getLogger(__name__)

# Channel config loaded from discord_client at startup
_channel_configs: dict[int, dict] = {}

# Discord bot reference (set by discord_client at startup)
_discord_bot: discord.Client | None = None


def set_discord_bot(bot: discord.Client) -> None:
    global _discord_bot
    _discord_bot = bot


def set_channel_configs(configs: dict[int, dict]) -> None:
    global _channel_configs
    _channel_configs = configs


def get_channel_name(channel_id: int) -> str | None:
    """Get channel folder name from config or thread registry."""
    cfg = _channel_configs.get(channel_id)
    if cfg:
        return cfg.get("_folder") or cfg.get("name")
    return state_manager.get_thread_folder(channel_id)


def find_attachments_for_message(message_id: int, channel_name: str | None = None) -> list[str]:
    if not channel_name:
        return []
    att_dir = attachments_dir(channel_name)
    if not att_dir.exists():
        return []
    return sorted(str(f) for f in att_dir.glob(f"msg_{message_id}_*"))


def check_for_new_messages(channel_id: int) -> list[dict]:
    """Check if new messages arrived since last check_messages call.

    Core of the new-message interrupt system.
    """
    last_seen = state_manager.get_last_seen(channel_id)
    if last_seen is None:
        return []

    db_path = state_manager.db_path
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
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
            """,
            (channel_id, last_seen, _config.WENDY_BOT_ID)
        ).fetchall()

        real_rows = [r for r in rows if r["message_id"] < SYNTHETIC_ID_THRESHOLD]
        if not real_rows:
            return []

        newest_id = max(r["message_id"] for r in real_rows)
        state_manager.update_last_seen(channel_id, newest_id)

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
    finally:
        conn.close()


def _validate_attachment_path(path_str: str) -> str | None:
    """Validate attachment path. Returns error message or None if valid."""
    att_path = Path(path_str).resolve()
    allowed_parents = [WENDY_BASE.resolve(), Path("/tmp").resolve()]
    if not any(att_path == parent or parent in att_path.parents for parent in allowed_parents):
        return f"Attachment must be in {WENDY_BASE}/ or /tmp/, got: {path_str}"
    if not att_path.exists():
        return f"Attachment file not found: {path_str}"
    return None


# =============================================================================
# Route handlers
# =============================================================================


async def handle_send_message(request: web.Request) -> web.Response:
    """POST /api/send_message -- send message to Discord directly."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    channel_id_str = body.get("channel_id")
    if not channel_id_str:
        return web.json_response({"error": "channel_id required"}, status=400)

    try:
        channel_id = int(channel_id_str)
    except ValueError:
        return web.json_response({"error": "Invalid channel_id"}, status=400)

    # Check for new messages (interrupt system)
    force = bool(body.get("force", False))
    if not force:
        new_messages = check_for_new_messages(channel_id)
        if new_messages:
            return web.json_response({
                "error": "New messages received since your last check. Review them and retry.",
                "new_messages": new_messages,
                "guidance": (
                    "Prefer sending ONE message that responds to all users at once, "
                    "rather than one message per person. Edit your reply to address everyone, then retry. "
                    "IMPORTANT: Do NOT mention this to users. No references to 'catching duplicates', "
                    "'interrupts', 'warnings', or anything about internal systems. Just naturally respond to everyone."
                ),
            })

    if not _discord_bot:
        return web.json_response({"error": "Discord bot not ready"}, status=503)

    channel = _discord_bot.get_channel(channel_id)
    if not channel:
        return web.json_response({"error": f"Channel {channel_id} not found"}, status=404)

    # Batch actions mode
    actions = body.get("actions")
    if actions:
        results = []
        for i, action in enumerate(actions):
            action_type = action.get("type")
            if action_type == "send_message":
                text = action.get("content", "")
                if len(text) > DISCORD_MAX_MESSAGE_LENGTH:
                    return web.json_response(
                        {"error": f"Action {i}: message too long ({len(text)} chars)"},
                        status=400,
                    )
                att_path = action.get("file_path") or action.get("attachment")
                if att_path:
                    err = _validate_attachment_path(att_path)
                    if err:
                        return web.json_response({"error": f"Action {i}: {err}"}, status=400)

                import discord
                file_obj = None
                if att_path:
                    file_obj = discord.File(att_path)

                reply_ref = None
                reply_to = action.get("reply_to")
                if reply_to:
                    reply_ref = discord.MessageReference(message_id=int(reply_to), channel_id=channel_id)

                await channel.send(content=text or None, file=file_obj, reference=reply_ref)
                results.append({"action": i, "type": "send_message", "success": True})

            elif action_type == "add_reaction":
                msg_id = action.get("message_id")
                emoji = action.get("emoji")
                if not msg_id or not emoji:
                    return web.json_response(
                        {"error": f"Action {i}: add_reaction requires message_id and emoji"},
                        status=400,
                    )
                try:
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.add_reaction(emoji)
                    results.append({"action": i, "type": "add_reaction", "success": True})
                except Exception as e:
                    results.append({"action": i, "type": "add_reaction", "error": str(e)})
            else:
                return web.json_response(
                    {"error": f"Action {i}: unknown type '{action_type}'"},
                    status=400,
                )

        new_messages = check_for_new_messages(channel_id)
        return web.json_response({"success": True, "results": results, "new_messages": new_messages})

    # Single message mode
    msg_text = body.get("content") or body.get("message") or ""
    if len(msg_text) > DISCORD_MAX_MESSAGE_LENGTH:
        return web.json_response(
            {"error": f"Message too long ({len(msg_text)} chars). Discord limit is {DISCORD_MAX_MESSAGE_LENGTH}."},
            status=400,
        )

    attachment = body.get("attachment")
    if attachment:
        err = _validate_attachment_path(attachment)
        if err:
            return web.json_response({"error": err}, status=400)

    import discord
    file_obj = None
    if attachment:
        file_obj = discord.File(attachment)

    reply_ref = None
    reply_to = body.get("reply_to")
    if reply_to:
        reply_ref = discord.MessageReference(message_id=int(reply_to), channel_id=channel_id)

    await channel.send(content=msg_text or None, file=file_obj, reference=reply_ref)
    new_messages = check_for_new_messages(channel_id)
    return web.json_response({"success": True, "message": "Message sent", "new_messages": new_messages})


async def handle_check_messages(request: web.Request) -> web.Response:
    """GET /api/check_messages/{channel_id} -- fetch recent messages and task updates."""
    try:
        channel_id = int(request.match_info["channel_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid channel_id"}, status=400)

    limit = min(int(request.query.get("limit", "10")), MAX_MESSAGE_LIMIT)
    all_messages = request.query.get("all_messages", "").lower() == "true"
    count_param = request.query.get("count")
    count = min(int(count_param), MAX_MESSAGE_LIMIT) if count_param else None

    channel_name = get_channel_name(channel_id)
    messages = []
    task_updates = []

    # Get messages from database
    try:
        db_path = state_manager.db_path
        if db_path.exists():
            if count is not None:
                since_id = None
                limit = count
            else:
                since_id = None if all_messages else state_manager.get_last_seen(channel_id)

            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                if since_id:
                    rows = conn.execute(
                        """
                        SELECT m.message_id, m.author_nickname, m.content, m.timestamp,
                               m.reply_to_id,
                               r.author_nickname as reply_author,
                               r.content as reply_content
                        FROM message_history m
                        LEFT JOIN message_history r ON m.reply_to_id = r.message_id
                        WHERE m.channel_id = ? AND m.message_id > ?
                        AND m.author_id != ?
                        AND m.content NOT LIKE '!%' AND m.content NOT LIKE '-%'
                        ORDER BY m.message_id DESC LIMIT ?
                        """,
                        (channel_id, since_id, _config.WENDY_BOT_ID, limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT m.message_id, m.author_nickname, m.content, m.timestamp,
                               m.reply_to_id,
                               r.author_nickname as reply_author,
                               r.content as reply_content
                        FROM message_history m
                        LEFT JOIN message_history r ON m.reply_to_id = r.message_id
                        WHERE m.channel_id = ?
                        AND m.author_id != ?
                        AND m.content NOT LIKE '!%' AND m.content NOT LIKE '-%'
                        ORDER BY m.message_id DESC LIMIT ?
                        """,
                        (channel_id, _config.WENDY_BOT_ID, limit)
                    ).fetchall()

                for row in rows:
                    attachments = find_attachments_for_message(row["message_id"], channel_name)
                    msg: dict = {
                        "message_id": row["message_id"],
                        "author": row["author_nickname"],
                        "content": row["content"],
                        "timestamp": row["timestamp"],
                    }
                    if attachments:
                        msg["attachments"] = attachments
                    if row["reply_to_id"] and row["reply_author"]:
                        msg["reply_to"] = {
                            "message_id": row["reply_to_id"],
                            "author": row["reply_author"],
                            "content": row["reply_content"] or "",
                        }
                    messages.append(msg)

                # Chronological order
                messages.reverse()

                # Separate synthetic from real
                synthetic_ids = [m["message_id"] for m in messages if m["message_id"] >= SYNTHETIC_ID_THRESHOLD]
                real_messages = [m for m in messages if m["message_id"] < SYNTHETIC_ID_THRESHOLD]

                if real_messages:
                    newest_id = max(m["message_id"] for m in real_messages)
                    state_manager.update_last_seen(channel_id, newest_id)

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
        _LOG.error("Error reading messages: %s", e)

    # Get task completion notifications
    try:
        unseen = state_manager.get_unseen_notifications_for_proxy()
        notification_ids = []
        for n in unseen:
            notification_ids.append(n.id)
            if n.type == "task_completion" and n.payload:
                task_updates.append({
                    "task_id": n.payload.get("task_id", "unknown"),
                    "title": n.title,
                    "status": n.payload.get("status", "completed"),
                    "duration": n.payload.get("duration", "unknown"),
                    "completed_at": n.created_at,
                })
        if notification_ids:
            state_manager.mark_notifications_seen_by_proxy(notification_ids)
    except Exception as e:
        _LOG.error("Error reading notifications: %s", e)

    return web.json_response({"messages": messages, "task_updates": task_updates})


async def handle_emojis(request: web.Request) -> web.Response:
    """GET /api/emojis -- list custom server emojis."""
    emoji_cache = SHARED_DIR / "emojis.json"
    if not emoji_cache.exists():
        return web.json_response({"custom": []})

    try:
        emojis = json.loads(emoji_cache.read_text())
    except (json.JSONDecodeError, OSError):
        return web.json_response({"custom": []})

    search = request.query.get("search")
    if search:
        term = search.lower()
        emojis = [e for e in emojis if term in e.get("name", "").lower()]

    return web.json_response({"custom": emojis})


# =============================================================================
# Deploy proxy endpoints
# =============================================================================

WENDY_WEB_URL = os.getenv("WENDY_WEB_URL", "http://localhost:8910")
WENDY_DEPLOY_TOKEN = os.getenv("WENDY_DEPLOY_TOKEN", "")
WENDY_GAMES_TOKEN = os.getenv("WENDY_GAMES_TOKEN", WENDY_DEPLOY_TOKEN)


async def handle_deploy_site(request: web.Request) -> web.Response:
    """POST /api/deploy_site -- proxy deploy to wendy-web."""
    if not WENDY_DEPLOY_TOKEN:
        return web.json_response({"error": "WENDY_DEPLOY_TOKEN not configured"}, status=500)

    try:
        reader = await request.multipart()
        name = None
        file_content = None

        async for part in reader:
            if part.name == "name":
                name = (await part.read()).decode()
            elif part.name == "files":
                file_content = await part.read()

        if not name or file_content is None:
            return web.json_response({"error": "name and files fields required"}, status=400)

        form = aiohttp.FormData()
        form.add_field("name", name)
        form.add_field("files", file_content, filename="site.tar.gz", content_type="application/gzip")

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.post(
                f"{WENDY_WEB_URL}/api/sites/deploy",
                data=form,
                headers={"Authorization": f"Bearer {WENDY_DEPLOY_TOKEN}"},
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    return web.json_response({"error": f"Deploy failed: {detail}"}, status=resp.status)
                result = await resp.json()

        return web.json_response({
            "success": True,
            "url": result.get("url"),
            "message": result.get("message", "Site deployed"),
        })
    except aiohttp.ClientError as e:
        return web.json_response({"error": f"Cannot connect to wendy-web: {e}"}, status=502)
    except Exception as e:
        _LOG.error("deploy_site error: %s", e)
        return web.json_response({"error": "Internal server error"}, status=500)


async def handle_deploy_game(request: web.Request) -> web.Response:
    """POST /api/deploy_game -- proxy deploy to wendy-web."""
    if not WENDY_GAMES_TOKEN:
        return web.json_response({"error": "WENDY_GAMES_TOKEN not configured"}, status=500)

    try:
        reader = await request.multipart()
        name = None
        file_content = None

        async for part in reader:
            if part.name == "name":
                name = (await part.read()).decode()
            elif part.name == "files":
                file_content = await part.read()

        if not name or file_content is None:
            return web.json_response({"error": "name and files fields required"}, status=400)

        form = aiohttp.FormData()
        form.add_field("name", name)
        form.add_field("files", file_content, filename="game.tar.gz", content_type="application/gzip")

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.post(
                f"{WENDY_WEB_URL}/api/games/deploy",
                data=form,
                headers={"Authorization": f"Bearer {WENDY_GAMES_TOKEN}"},
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    return web.json_response({"error": f"Deploy failed: {detail}"}, status=resp.status)
                result = await resp.json()

        return web.json_response({
            "success": True,
            "url": result.get("url"),
            "ws": result.get("ws"),
            "port": result.get("port"),
            "message": result.get("message", "Game deployed"),
        })
    except aiohttp.ClientError as e:
        return web.json_response({"error": f"Cannot connect to wendy-web: {e}"}, status=502)
    except Exception as e:
        _LOG.error("deploy_game error: %s", e)
        return web.json_response({"error": "Internal server error"}, status=500)


async def handle_game_logs(request: web.Request) -> web.Response:
    """GET /api/game_logs/{name} -- fetch game server logs."""
    name = request.match_info["name"]
    lines = int(request.query.get("lines", "100"))

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(
                f"{WENDY_WEB_URL}/api/games/{name}/logs",
                params={"lines": lines},
                headers={"Authorization": f"Bearer {WENDY_GAMES_TOKEN}"},
            ) as resp:
                if resp.status == 404:
                    return web.json_response({"name": name, "logs": f"Game '{name}' not found"})
                if resp.status != 200:
                    return web.json_response({"name": name, "logs": f"Error: {await resp.text()}"})
                return web.json_response(await resp.json())
    except aiohttp.ClientError as e:
        return web.json_response({"name": name, "logs": f"Connection error: {e}"})


# =============================================================================
# Gemini file analysis
# =============================================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MAX_FILE_SIZE = 20 * 1024 * 1024
GEMINI_MAX_VIDEO_DURATION = 5 * 60
GEMINI_MAX_AUDIO_DURATION = 30 * 60

SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
SUPPORTED_AUDIO_TYPES = {"audio/wav", "audio/mp3", "audio/mpeg", "audio/aiff", "audio/aac", "audio/ogg", "audio/flac"}
SUPPORTED_VIDEO_TYPES = {"video/mp4", "video/mpeg", "video/quicktime", "video/avi", "video/x-flv", "video/webm", "video/x-ms-wmv", "video/3gpp"}
SUPPORTED_MEDIA_TYPES = SUPPORTED_IMAGE_TYPES | SUPPORTED_AUDIO_TYPES | SUPPORTED_VIDEO_TYPES

EXTENSION_TO_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".aiff": "audio/aiff",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
    ".mov": "video/quicktime", ".avi": "video/avi", ".flv": "video/x-flv",
    ".webm": "video/webm", ".wmv": "video/x-ms-wmv", ".3gp": "video/3gpp",
}


def _infer_media_type(filename: str | None, content_type: str | None) -> str:
    if content_type and content_type != "application/octet-stream":
        return content_type
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in EXTENSION_TO_MIME:
            return EXTENSION_TO_MIME[ext]
    return content_type or ""


def _get_media_duration(content: bytes, media_type: str) -> float | None:
    if media_type not in SUPPORTED_AUDIO_TYPES and media_type not in SUPPORTED_VIDEO_TYPES:
        return None
    try:
        suffix = ".mp4" if media_type.startswith("video/") else ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
            temp_path = f.name
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", temp_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        finally:
            Path(temp_path).unlink(missing_ok=True)
    except Exception:
        pass
    return None


def _get_gemini_model(media_type: str) -> str:
    return "gemini-2.5-pro" if media_type in SUPPORTED_VIDEO_TYPES else "gemini-3-pro-preview"


def _get_video_resolution(duration: float | None) -> str:
    if duration is None:
        return "MEDIA_RESOLUTION_MEDIUM"
    if duration <= 30:
        return "MEDIA_RESOLUTION_HIGH"
    if duration <= 120:
        return "MEDIA_RESOLUTION_MEDIUM"
    return "MEDIA_RESOLUTION_LOW"


async def handle_analyze_file(request: web.Request) -> web.Response:
    """POST /api/analyze_file -- analyze media via Gemini API."""
    if not GEMINI_API_KEY:
        return web.json_response({"error": "GEMINI_API_KEY not configured"}, status=500)

    try:
        reader = await request.multipart()
        prompt = None
        file_content = None
        filename = None
        content_type = None

        async for part in reader:
            if part.name == "prompt":
                prompt = (await part.read()).decode()
            elif part.name == "file":
                filename = part.filename
                content_type = part.headers.get("Content-Type")
                file_content = await part.read()

        if not prompt or file_content is None:
            return web.json_response({"error": "prompt and file fields required"}, status=400)

        media_type = _infer_media_type(filename, content_type)
        if media_type not in SUPPORTED_MEDIA_TYPES:
            return web.json_response({"error": f"Unsupported file type: {media_type}"}, status=400)

        if len(file_content) > GEMINI_MAX_FILE_SIZE:
            return web.json_response(
                {"error": f"File too large ({len(file_content) / 1024 / 1024:.1f}MB). Max 20MB."},
                status=400,
            )

        duration = _get_media_duration(file_content, media_type)
        if duration is not None:
            if media_type in SUPPORTED_VIDEO_TYPES and duration > GEMINI_MAX_VIDEO_DURATION:
                return web.json_response({"error": f"Video too long ({duration / 60:.1f} min). Max 5 min."}, status=400)
            if media_type in SUPPORTED_AUDIO_TYPES and duration > GEMINI_MAX_AUDIO_DURATION:
                return web.json_response({"error": f"Audio too long ({duration / 60:.1f} min). Max 30 min."}, status=400)

        model = _get_gemini_model(media_type)
        file_b64 = base64.standard_b64encode(file_content).decode()
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

        body: dict = {
            "contents": [{"parts": [
                {"inline_data": {"mime_type": media_type, "data": file_b64}},
                {"text": prompt},
            ]}]
        }
        if media_type in SUPPORTED_VIDEO_TYPES:
            body["generation_config"] = {"media_resolution": _get_video_resolution(duration)}

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.post(
                gemini_url, headers={"x-goog-api-key": GEMINI_API_KEY}, json=body,
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    return web.json_response({"error": f"Gemini API error: {detail}"}, status=502)
                result = await resp.json()

        try:
            analysis = result["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return web.json_response({"error": f"Unexpected Gemini response: {result}"}, status=502)

        return web.json_response({
            "success": True, "analysis": analysis, "media_type": media_type, "model": model,
        })
    except aiohttp.ClientError as e:
        return web.json_response({"error": f"Gemini connection error: {e}"}, status=502)
    except Exception as e:
        _LOG.error("analyze_file error: %s", e)
        return web.json_response({"error": "Internal server error"}, status=500)


# =============================================================================
# Usage tracking
# =============================================================================

USAGE_DATA_FILE = WENDY_BASE / "usage_data.json"
USAGE_FORCE_CHECK_FILE = WENDY_BASE / "usage_force_check"


async def handle_usage(request: web.Request) -> web.Response:
    """GET /api/usage -- Claude Code usage stats."""
    if not USAGE_DATA_FILE.exists():
        return web.json_response({"error": "Usage data not available yet"}, status=404)
    try:
        data = json.loads(USAGE_DATA_FILE.read_text())
        week_all = data.get("week_all_percent", 0)
        week_sonnet = data.get("week_sonnet_percent", 0)
        updated = data.get("updated_at", "unknown")
        data["message"] = (
            f"Claude Code Usage (as of {updated}):\n"
            f"- Weekly (all models): {week_all}%\n"
            f"- Weekly (Sonnet only): {week_sonnet}%"
        )
        return web.json_response(data)
    except Exception as e:
        _LOG.error("usage error: %s", e)
        return web.json_response({"error": "Failed to read usage data"}, status=500)


async def handle_usage_refresh(request: web.Request) -> web.Response:
    """POST /api/usage/refresh -- force immediate usage check."""
    try:
        USAGE_FORCE_CHECK_FILE.touch()
        return web.json_response({"success": True, "message": "Usage refresh requested. Check back in ~30s."})
    except Exception as e:
        _LOG.error("usage refresh error: %s", e)
        return web.json_response({"error": "Internal server error"}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    """GET /health"""
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application(client_max_size=30 * 1024 * 1024)  # 30MB for file uploads
    app.router.add_post("/api/send_message", handle_send_message)
    app.router.add_get("/api/check_messages/{channel_id}", handle_check_messages)
    app.router.add_get("/api/emojis", handle_emojis)
    app.router.add_post("/api/deploy_site", handle_deploy_site)
    app.router.add_post("/api/deploy_game", handle_deploy_game)
    app.router.add_get("/api/game_logs/{name}", handle_game_logs)
    app.router.add_post("/api/analyze_file", handle_analyze_file)
    app.router.add_get("/api/usage", handle_usage)
    app.router.add_post("/api/usage/refresh", handle_usage_refresh)
    app.router.add_get("/health", handle_health)
    return app


async def start_server(port: int) -> web.AppRunner:
    """Start the HTTP server and return the runner (for cleanup)."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    _LOG.info("API server listening on port %d", port)
    return runner
