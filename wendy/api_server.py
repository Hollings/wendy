"""Internal HTTP API server for the Claude CLI subprocess.

Runs as an aiohttp application inside the bot process. Claude CLI calls these
endpoints via ``curl`` to send Discord messages, read message history, deploy
sites/games, analyse media through Gemini, and check usage stats.

Route overview (see ``create_app`` for the full route table):
    POST /api/send_message          -- send or batch-send Discord messages
    GET  /api/check_messages/:id    -- fetch recent messages from SQLite
    GET  /api/emojis                -- search custom server emojis
    POST /api/deploy_site           -- proxy a static-site deploy to wendy-web
    POST /api/deploy_game           -- proxy a game deploy to wendy-web
    GET  /api/game_logs/:name       -- fetch game server logs
    POST /api/analyze_file          -- analyse media via Gemini
    GET  /api/usage                 -- Claude Code usage stats
    POST /api/usage/refresh         -- force a usage data refresh
    POST /api/feature_request       -- submit a feature request
    GET  /api/feature_requests      -- list feature requests (default: pending)
    POST /api/feature_request/resolve -- resolve/reject a feature request
    GET  /health                    -- liveness check
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
from datetime import UTC, datetime
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
from .paths import SHARED_DIR, WENDY_BASE, find_attachments_for_message
from .state import state as state_manager

if TYPE_CHECKING:
    import discord

_LOG = logging.getLogger(__name__)

# Channel config loaded from discord_client at startup
_channel_configs: dict[int, dict] = {}

# Discord bot reference (set by discord_client at startup)
_discord_bot: discord.Client | None = None


def set_discord_bot(bot: discord.Client) -> None:
    """Store a reference to the Discord bot so route handlers can send messages."""
    global _discord_bot
    _discord_bot = bot


def set_channel_configs(configs: dict[int, dict]) -> None:
    """Update the channel configuration lookup (called on startup and config reload)."""
    global _channel_configs
    _channel_configs = configs


def _is_enrichment_active(channel_id: int) -> bool:
    """Return True if Wendy is currently in an enrichment session for this channel."""
    if _discord_bot and hasattr(_discord_bot, "is_enrichment_active"):
        return _discord_bot.is_enrichment_active(channel_id)
    return False


def get_channel_name(channel_id: int) -> str | None:
    """Get channel folder name from config or thread registry."""
    cfg = _channel_configs.get(channel_id)
    if cfg:
        return cfg.get("_folder") or cfg.get("name")
    return state_manager.get_thread_folder(channel_id)


def check_for_new_messages(channel_id: int) -> list[dict]:
    """Return new *real* messages since the last ``check_messages`` call.

    Thin wrapper around ``state_manager.check_for_new_messages`` that passes
    the bot user ID and config constants.
    """
    return state_manager.check_for_new_messages(
        channel_id,
        bot_user_id=_config.WENDY_BOT_ID,
        synthetic_id_threshold=SYNTHETIC_ID_THRESHOLD,
        max_limit=MAX_MESSAGE_LIMIT,
    )


def _save_bot_message(msg: discord.Message | None, channel_id: int) -> None:
    """Persist a bot-sent Discord message to SQLite for history and check_messages visibility."""
    if not msg:
        return
    try:
        state_manager.insert_message(
            message_id=msg.id,
            channel_id=channel_id,
            guild_id=msg.guild.id if msg.guild else None,
            author_id=msg.author.id,
            author_nickname=msg.author.display_name,
            is_bot=True,
            content=msg.content or "",
            timestamp=int(msg.created_at.timestamp()),
        )
    except Exception as e:
        _LOG.warning("Failed to save bot message %s: %s", msg.id, e)


def _validate_attachment_path(path_str: str) -> str | None:
    """Validate that *path_str* lives under an allowed directory and exists.

    Returns an error string on failure, or ``None`` when valid.
    """
    att_path = Path(path_str).resolve()
    allowed_parents = [WENDY_BASE.resolve(), Path("/tmp").resolve()]
    if not any(att_path == parent or parent in att_path.parents for parent in allowed_parents):
        return f"Attachment must be in {WENDY_BASE}/ or /tmp/, got: {path_str}"
    if not att_path.exists():
        return f"Attachment file not found: {path_str}"
    return None


def _build_discord_send_kwargs(
    body: dict,
    channel_id: int,
) -> tuple[dict, str | None]:
    """Build ``channel.send()`` keyword arguments from a request body.

    Handles content, attachment validation, and reply references.  Shared by
    both single-message and batch-action ``send_message`` paths.

    Returns ``(kwargs_dict, error_string)``.  *error_string* is ``None`` when
    the input is valid.
    """
    import discord as _discord

    text = body.get("content") or body.get("message") or ""
    if len(text) > DISCORD_MAX_MESSAGE_LENGTH:
        return {}, f"Message too long ({len(text)} chars). Discord limit is {DISCORD_MAX_MESSAGE_LENGTH}."

    att_path = body.get("file_path") or body.get("attachment")
    if att_path:
        err = _validate_attachment_path(att_path)
        if err:
            return {}, err

    kwargs: dict = {"content": text or None}
    if att_path:
        kwargs["file"] = _discord.File(att_path)

    reply_to = body.get("reply_to")
    if reply_to:
        kwargs["reference"] = _discord.MessageReference(
            message_id=int(reply_to), channel_id=channel_id,
        )

    return kwargs, None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _parse_channel_id(body: dict) -> tuple[int | None, web.Response | None]:
    """Extract and validate ``channel_id`` from a JSON body.

    Returns ``(channel_id, None)`` on success or ``(None, error_response)``
    on failure.
    """
    raw = body.get("channel_id")
    if not raw:
        return None, web.json_response({"error": "channel_id required"}, status=400)
    try:
        return int(raw), None
    except ValueError:
        return None, web.json_response({"error": "Invalid channel_id"}, status=400)


async def _execute_batch_actions(
    actions: list[dict],
    channel: discord.TextChannel,
    channel_id: int,
) -> web.Response:
    """Process a list of batch actions (send_message / add_reaction).

    Returns a JSON response with per-action results.
    """
    results: list[dict] = []
    for i, action in enumerate(actions):
        action_type = action.get("type")

        if action_type == "send_message":
            kwargs, err = _build_discord_send_kwargs(action, channel_id)
            if err:
                return web.json_response({"error": f"Action {i}: {err}"}, status=400)
            sent_msg = await channel.send(**kwargs)
            _save_bot_message(sent_msg, channel_id)
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
                {"error": f"Action {i}: unknown type '{action_type}'"}, status=400,
            )

    new_messages = check_for_new_messages(channel_id)
    return web.json_response({"success": True, "results": results, "new_messages": new_messages})


async def handle_send_message(request: web.Request) -> web.Response:
    """POST /api/send_message -- send a message (or batch) to Discord."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    channel_id, err_resp = _parse_channel_id(body)
    if err_resp:
        return err_resp

    if _is_enrichment_active(channel_id):
        return web.json_response(
            {"error": "send_message is blocked during lunch break. This is personal time -- no Discord."},
            status=403,
        )

    # Interrupt system: surface unseen messages before allowing a send.
    if not body.get("force", False):
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

    # Batch mode
    actions = body.get("actions")
    if actions:
        return await _execute_batch_actions(actions, channel, channel_id)

    # Single message mode
    kwargs, err = _build_discord_send_kwargs(body, channel_id)
    if err:
        return web.json_response({"error": err}, status=400)

    sent_msg = await channel.send(**kwargs)
    _save_bot_message(sent_msg, channel_id)
    new_messages = check_for_new_messages(channel_id)
    return web.json_response({"success": True, "message": "Message sent", "new_messages": new_messages})


def _delete_synthetic_messages(synthetic_ids: list[int]) -> None:
    """Delete consumed synthetic messages from the database."""
    state_manager.delete_messages(synthetic_ids)


def _collect_task_updates() -> list[dict]:
    """Consume unseen task-completion notifications and return them as dicts."""
    unseen = state_manager.get_unseen_notifications_for_proxy()
    notification_ids: list[int] = []
    task_updates: list[dict] = []
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
    return task_updates


async def handle_check_messages(request: web.Request) -> web.Response:
    """GET /api/check_messages/{channel_id} -- fetch recent messages and task updates.

    Query parameters:
        limit         -- max messages to return (default 10, capped by MAX_MESSAGE_LIMIT)
        all_messages  -- ``true`` to ignore the last-seen watermark
        count         -- override *limit* and ignore last-seen (fetch latest N)
    """
    try:
        channel_id = int(request.match_info["channel_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid channel_id"}, status=400)

    if _is_enrichment_active(channel_id):
        return web.json_response(
            {"error": "check_messages is blocked during lunch break. This is personal time -- no Discord."},
            status=403,
        )

    limit = min(int(request.query.get("limit", "10")), MAX_MESSAGE_LIMIT)
    all_messages = request.query.get("all_messages", "").lower() == "true"
    count_param = request.query.get("count")
    count = min(int(count_param), MAX_MESSAGE_LIMIT) if count_param else None

    channel_name = get_channel_name(channel_id)
    messages: list[dict] = []
    task_updates: list[dict] = []

    # --- Messages ---
    try:
        if count is not None:
            since_id = None
            limit = count
        else:
            since_id = None if all_messages else state_manager.get_last_seen(channel_id)

        rows = state_manager.fetch_messages(
            channel_id, since_id=since_id, limit=limit,
        )
        messages = [
            state_manager._row_to_message_dict(
                r,
                attachment_paths=find_attachments_for_message(r["message_id"], channel_name),
            )
            for r in rows
        ]

        # Rows come back DESC; reverse to chronological order.
        messages.reverse()

        # Advance the watermark for real messages; clean up consumed synthetics.
        synthetic_ids = [m["message_id"] for m in messages if m["message_id"] >= SYNTHETIC_ID_THRESHOLD]
        real_messages = [m for m in messages if m["message_id"] < SYNTHETIC_ID_THRESHOLD]
        if real_messages:
            state_manager.update_last_seen(channel_id, max(m["message_id"] for m in real_messages))
        _delete_synthetic_messages(synthetic_ids)

    except Exception as e:
        _LOG.error("Error reading messages: %s", e)

    # --- Task updates ---
    try:
        task_updates = _collect_task_updates()
    except Exception as e:
        _LOG.error("Error reading notifications: %s", e)

    return web.json_response({"messages": messages, "task_updates": task_updates})


async def handle_emojis(request: web.Request) -> web.Response:
    """GET /api/emojis -- list custom server emojis.

    Query parameters:
        search -- case-insensitive substring filter on emoji name
    """
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


# ---------------------------------------------------------------------------
# Deploy proxy endpoints
# ---------------------------------------------------------------------------

WENDY_WEB_URL = os.getenv("WENDY_WEB_URL", "http://localhost:8910")
WENDY_DEPLOY_TOKEN = os.getenv("WENDY_DEPLOY_TOKEN", "")
WENDY_GAMES_TOKEN = os.getenv("WENDY_GAMES_TOKEN", WENDY_DEPLOY_TOKEN)


async def _read_multipart_name_and_files(request: web.Request) -> tuple[str | None, bytes | None]:
    """Parse a multipart request containing ``name`` and ``files`` fields.

    Returns ``(name, file_bytes)`` -- either value may be ``None`` if the
    corresponding field was missing.
    """
    reader = await request.multipart()
    name: str | None = None
    file_content: bytes | None = None
    async for part in reader:
        if part.name == "name":
            name = (await part.read()).decode()
        elif part.name == "files":
            file_content = await part.read()
    return name, file_content


async def _proxy_deploy(
    request: web.Request,
    *,
    token: str,
    token_env_name: str,
    deploy_path: str,
    archive_filename: str,
    timeout: int,
    extra_response_keys: tuple[str, ...] = (),
    default_message: str = "Deployed",
) -> web.Response:
    """Shared logic for proxying a deploy request to wendy-web.

    Reads a multipart ``name`` + ``files`` payload, re-packages it, and
    forwards it to *deploy_path* on the wendy-web service.
    """
    if not token:
        return web.json_response({"error": f"{token_env_name} not configured"}, status=500)

    try:
        name, file_content = await _read_multipart_name_and_files(request)
        if not name or file_content is None:
            return web.json_response({"error": "name and files fields required"}, status=400)

        form = aiohttp.FormData()
        form.add_field("name", name)
        form.add_field("files", file_content, filename=archive_filename, content_type="application/gzip")

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(
                f"{WENDY_WEB_URL}{deploy_path}",
                data=form,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    return web.json_response({"error": f"Deploy failed: {detail}"}, status=resp.status)
                result = await resp.json()

        body: dict = {
            "success": True,
            "url": result.get("url"),
            "message": result.get("message", default_message),
        }
        for key in extra_response_keys:
            body[key] = result.get(key)
        return web.json_response(body)

    except aiohttp.ClientError as e:
        return web.json_response({"error": f"Cannot connect to wendy-web: {e}"}, status=502)
    except Exception as e:
        _LOG.error("deploy error (%s): %s", deploy_path, e)
        return web.json_response({"error": "Internal server error"}, status=500)


async def handle_deploy_site(request: web.Request) -> web.Response:
    """POST /api/deploy_site -- proxy a static-site deploy to wendy-web."""
    return await _proxy_deploy(
        request,
        token=WENDY_DEPLOY_TOKEN,
        token_env_name="WENDY_DEPLOY_TOKEN",
        deploy_path="/api/sites/deploy",
        archive_filename="site.tar.gz",
        timeout=60,
        default_message="Site deployed",
    )


async def handle_deploy_game(request: web.Request) -> web.Response:
    """POST /api/deploy_game -- proxy a game deploy to wendy-web."""
    return await _proxy_deploy(
        request,
        token=WENDY_GAMES_TOKEN,
        token_env_name="WENDY_GAMES_TOKEN",
        deploy_path="/api/games/deploy",
        archive_filename="game.tar.gz",
        timeout=120,
        extra_response_keys=("ws", "port"),
        default_message="Game deployed",
    )


async def handle_game_logs(request: web.Request) -> web.Response:
    """GET /api/game_logs/{name} -- proxy game log retrieval from wendy-web.

    Query parameters:
        lines -- number of log lines to return (default 100)
    """
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


# ---------------------------------------------------------------------------
# Gemini file analysis
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
GEMINI_MAX_VIDEO_DURATION = 5 * 60       # 5 minutes
GEMINI_MAX_AUDIO_DURATION = 30 * 60      # 30 minutes

SUPPORTED_IMAGE_TYPES = {
    "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif",
}
SUPPORTED_AUDIO_TYPES = {
    "audio/wav", "audio/mp3", "audio/mpeg", "audio/aiff",
    "audio/aac", "audio/ogg", "audio/flac",
}
SUPPORTED_VIDEO_TYPES = {
    "video/mp4", "video/mpeg", "video/quicktime", "video/avi",
    "video/x-flv", "video/webm", "video/x-ms-wmv", "video/3gpp",
}
SUPPORTED_MEDIA_TYPES = SUPPORTED_IMAGE_TYPES | SUPPORTED_AUDIO_TYPES | SUPPORTED_VIDEO_TYPES

EXTENSION_TO_MIME: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".aiff": "audio/aiff",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
    ".mov": "video/quicktime", ".avi": "video/avi", ".flv": "video/x-flv",
    ".webm": "video/webm", ".wmv": "video/x-ms-wmv", ".3gp": "video/3gpp",
}


def _infer_media_type(filename: str | None, content_type: str | None) -> str:
    """Determine MIME type from the Content-Type header or file extension.

    Falls back to extension-based lookup when the header is missing or is the
    generic ``application/octet-stream``.
    """
    if content_type and content_type != "application/octet-stream":
        return content_type
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in EXTENSION_TO_MIME:
            return EXTENSION_TO_MIME[ext]
    return content_type or ""


def _get_media_duration(content: bytes, media_type: str) -> float | None:
    """Use ``ffprobe`` to determine duration of an audio or video file.

    Returns ``None`` for non-AV media or when ffprobe is unavailable.
    """
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
    """Select the Gemini model appropriate for the media type."""
    return "gemini-2.5-pro" if media_type in SUPPORTED_VIDEO_TYPES else "gemini-3-pro-preview"


def _get_video_resolution(duration: float | None) -> str:
    """Choose Gemini media resolution setting based on video duration."""
    if duration is None:
        return "MEDIA_RESOLUTION_MEDIUM"
    if duration <= 30:
        return "MEDIA_RESOLUTION_HIGH"
    if duration <= 120:
        return "MEDIA_RESOLUTION_MEDIUM"
    return "MEDIA_RESOLUTION_LOW"


def _validate_media(file_content: bytes, media_type: str) -> tuple[str | None, float | None]:
    """Check file size and duration limits.

    Returns ``(error_string, duration)``.  *error_string* is ``None`` when
    validation passes.  *duration* is the media duration in seconds (or
    ``None`` for images / when ffprobe is unavailable).
    """
    if len(file_content) > GEMINI_MAX_FILE_SIZE:
        return f"File too large ({len(file_content) / 1024 / 1024:.1f}MB). Max 20MB.", None

    duration = _get_media_duration(file_content, media_type)
    if duration is not None:
        if media_type in SUPPORTED_VIDEO_TYPES and duration > GEMINI_MAX_VIDEO_DURATION:
            return f"Video too long ({duration / 60:.1f} min). Max 5 min.", duration
        if media_type in SUPPORTED_AUDIO_TYPES and duration > GEMINI_MAX_AUDIO_DURATION:
            return f"Audio too long ({duration / 60:.1f} min). Max 30 min.", duration

    return None, duration


def _build_gemini_request_body(
    file_content: bytes,
    media_type: str,
    prompt: str,
    duration: float | None = None,
) -> dict:
    """Assemble the JSON body for the Gemini ``generateContent`` endpoint.

    *duration* should be pre-computed by ``_validate_media`` so ffprobe is not
    invoked a second time.
    """
    file_b64 = base64.standard_b64encode(file_content).decode()
    body: dict = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": media_type, "data": file_b64}},
            {"text": prompt},
        ]}],
    }
    if media_type in SUPPORTED_VIDEO_TYPES:
        body["generation_config"] = {"media_resolution": _get_video_resolution(duration)}
    return body


async def handle_analyze_file(request: web.Request) -> web.Response:
    """POST /api/analyze_file -- analyse media (image/audio/video) via Gemini.

    Expects a multipart form with ``prompt`` (text) and ``file`` (binary)
    fields.  Validates media type and size/duration, then proxies the request
    to the Gemini API and returns the analysis text.
    """
    if not GEMINI_API_KEY:
        return web.json_response({"error": "GEMINI_API_KEY not configured"}, status=500)

    try:
        reader = await request.multipart()
        prompt: str | None = None
        file_content: bytes | None = None
        filename: str | None = None
        content_type: str | None = None

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

        err, duration = _validate_media(file_content, media_type)
        if err:
            return web.json_response({"error": err}, status=400)

        model = _get_gemini_model(media_type)
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = _build_gemini_request_body(file_content, media_type, prompt, duration)

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


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

USAGE_DATA_FILE = WENDY_BASE / "usage_data.json"
USAGE_FORCE_CHECK_FILE = WENDY_BASE / "usage_force_check"


async def handle_usage(request: web.Request) -> web.Response:
    """GET /api/usage -- return Claude Code usage stats from the cached JSON file."""
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
    """POST /api/usage/refresh -- create a marker file to trigger an immediate usage check."""
    try:
        USAGE_FORCE_CHECK_FILE.touch()
        return web.json_response({"success": True, "message": "Usage refresh requested. Check back in ~30s."})
    except Exception as e:
        _LOG.error("usage refresh error: %s", e)
        return web.json_response({"error": "Internal server error"}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    """GET /health -- simple liveness probe."""
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# Feature requests
# ---------------------------------------------------------------------------

FEATURE_REQUESTS_FILE = SHARED_DIR / "feature_requests.json"


def _load_feature_requests() -> list[dict]:
    if not FEATURE_REQUESTS_FILE.exists():
        return []
    try:
        return json.loads(FEATURE_REQUESTS_FILE.read_text()).get("requests", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_feature_requests(requests: list[dict]) -> None:
    FEATURE_REQUESTS_FILE.write_text(json.dumps({"requests": requests}, indent=2))


async def handle_feature_request(request: web.Request) -> web.Response:
    """POST /api/feature_request -- submit a feature request."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    description = body.get("request")
    if not description:
        return web.json_response({"error": "request field required"}, status=400)

    requests = _load_feature_requests()
    new_id = max((r.get("id", 0) for r in requests), default=0) + 1
    requests.append({
        "id": new_id,
        "user": body.get("user", "unknown"),
        "request": description,
        "submitted_at": datetime.now(UTC).isoformat(),
        "status": "pending",
        "channel_id": str(body.get("channel_id", "")),
    })
    _save_feature_requests(requests)
    return web.json_response({"success": True, "id": new_id, "message": f"Feature request #{new_id} logged."})


async def handle_list_feature_requests(request: web.Request) -> web.Response:
    """GET /api/feature_requests -- list feature requests (default: pending only)."""
    status_filter = request.query.get("status", "pending")
    requests = _load_feature_requests()
    if status_filter != "all":
        requests = [r for r in requests if r.get("status") == status_filter]
    return web.json_response({"requests": requests})


async def handle_resolve_feature_request(request: web.Request) -> web.Response:
    """POST /api/feature_request/resolve -- mark a request as resolved/rejected."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    req_id = body.get("id")
    if req_id is None:
        return web.json_response({"error": "id field required"}, status=400)

    resolution = body.get("resolution", "resolved")
    requests = _load_feature_requests()
    for r in requests:
        if r.get("id") == req_id:
            r["status"] = resolution
            r["resolved_at"] = datetime.now(UTC).isoformat()
            break
    else:
        return web.json_response({"error": f"Request #{req_id} not found"}, status=404)

    _save_feature_requests(requests)
    return web.json_response({"success": True})


# ---------------------------------------------------------------------------
# Application factory and server startup
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    """Build the aiohttp ``Application`` with all API routes registered."""
    app = web.Application(client_max_size=30 * 1024 * 1024)  # 30 MB for file uploads
    app.router.add_post("/api/send_message", handle_send_message)
    app.router.add_get("/api/check_messages/{channel_id}", handle_check_messages)
    app.router.add_get("/api/emojis", handle_emojis)
    app.router.add_post("/api/deploy_site", handle_deploy_site)
    app.router.add_post("/api/deploy_game", handle_deploy_game)
    app.router.add_get("/api/game_logs/{name}", handle_game_logs)
    app.router.add_post("/api/analyze_file", handle_analyze_file)
    app.router.add_get("/api/usage", handle_usage)
    app.router.add_post("/api/usage/refresh", handle_usage_refresh)
    app.router.add_post("/api/feature_request", handle_feature_request)
    app.router.add_get("/api/feature_requests", handle_list_feature_requests)
    app.router.add_post("/api/feature_request/resolve", handle_resolve_feature_request)
    app.router.add_get("/health", handle_health)
    return app


async def start_server(port: int) -> web.AppRunner:
    """Start the HTTP server on *port* and return the ``AppRunner`` for cleanup."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    _LOG.info("API server listening on port %d", port)
    return runner
