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
    GET  /health                    -- liveness check
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from . import config as _config
from .config import (
    DISCORD_MAX_MESSAGE_LENGTH,
    MAX_MESSAGE_LIMIT,
    SYNTHETIC_ID_THRESHOLD,
)
from .deploy_proxy import handle_deploy_game, handle_deploy_site, handle_game_logs
from .gemini_analyzer import handle_analyze_file
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

    Uses Path.resolve() so that symlinks are fully expanded before the
    allowed-parent check: a symlink pointing outside the allowed tree will
    resolve to its real target and be caught here.
    """
    att_path = Path(path_str).resolve()
    allowed_parents = [WENDY_BASE.resolve(), Path("/tmp").resolve()]
    if not any(att_path.is_relative_to(parent) for parent in allowed_parents):
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
            results.append({
                "action": i, "type": "send_message", "success": True,
                "message_id": sent_msg.id, "content": sent_msg.content or "",
            })

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
    resp_body: dict = {
        "success": True,
        "message": "Message sent",
        "message_id": sent_msg.id,
        "content": sent_msg.content or "",
        "new_messages": new_messages,
    }
    if sent_msg.attachments:
        resp_body["attachments"] = [
            {"filename": a.filename, "size": a.size, "url": a.url}
            for a in sent_msg.attachments
        ]
    return web.json_response(resp_body)


async def handle_check_messages(request: web.Request) -> web.Response:
    """GET /api/check_messages/{channel_id} -- fetch recent messages.

    Query parameters:
        limit         -- max messages to return (default 10, capped by MAX_MESSAGE_LIMIT)
        all_messages  -- ``true`` to ignore the last-seen watermark
        count         -- override *limit* and ignore last-seen (fetch latest N)
        peek          -- ``true`` to read WITHOUT advancing the seen cursor or
                         marking synthetics delivered (a true non-destructive read)

    A normal (non-peek) read advances the per-channel seen cursor and marks the
    synthetic messages it returns as *delivered* rather than deleting them.  The
    orchestrator deletes delivered synthetics only when the turn completes, and
    un-marks them if the turn fails -- so a mid-turn crash never loses them.
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
    peek = request.query.get("peek", "").lower() == "true"
    count_param = request.query.get("count")
    count = min(int(count_param), MAX_MESSAGE_LIMIT) if count_param else None

    channel_name = get_channel_name(channel_id)
    messages: list[dict] = []

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

        # A peek is a true non-destructive read: leave the cursor and synthetics
        # exactly as they were.
        if not peek:
            synthetic_ids = [m["message_id"] for m in messages if m["message_id"] >= SYNTHETIC_ID_THRESHOLD]
            real_messages = [m for m in messages if m["message_id"] < SYNTHETIC_ID_THRESHOLD]
            if real_messages:
                state_manager.update_last_seen(channel_id, max(m["message_id"] for m in real_messages))
            # Mark (don't delete) synthetics so a crashed turn can re-read them.
            state_manager.mark_synthetics_delivered(synthetic_ids)

    except Exception as e:
        _LOG.error("Error reading messages: %s", e)

    # ``task_updates`` retained as an empty list for response-shape compatibility;
    # task completions now surface once, as synthetic messages via watch_notifications.
    return web.json_response({"messages": messages, "task_updates": []})


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
# Self-wake scheduling
# ---------------------------------------------------------------------------


async def handle_schedule_wake(request: web.Request) -> web.Response:
    """POST /api/schedule_wake -- schedule a delayed self-wake for a channel."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    channel_id = data.get("channel_id")
    delay = data.get("delay_seconds")
    message = data.get("message", "")

    if not channel_id or not delay:
        return web.json_response({"error": "channel_id and delay_seconds required"}, status=400)

    try:
        channel_id = int(channel_id)
        delay = int(delay)
    except (ValueError, TypeError):
        return web.json_response({"error": "channel_id and delay_seconds must be integers"}, status=400)

    if delay < 10 or delay > 86400:
        return web.json_response({"error": "delay must be between 10 seconds and 24 hours"}, status=400)

    if not _discord_bot or not hasattr(_discord_bot, "schedule_wake"):
        return web.json_response({"error": "bot not available"}, status=503)

    wake_time = _discord_bot.schedule_wake(channel_id, delay, message)
    return web.json_response({"success": True, "wake_time": wake_time})


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
    app.router.add_post("/api/schedule_wake", handle_schedule_wake)
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
