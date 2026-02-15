"""Outbox watcher for Wendy's async message sending.

This module implements a file-watching cog that monitors /data/wendy/shared/outbox/
for JSON files and sends them as Discord messages. This allows Claude CLI
(via the proxy API) to queue messages without direct Discord access.

File Format:
    Outbox files are JSON with the following structure:
    {
        "channel_id": "123456789",
        "message": "Hello world",
        "file_path": "/data/wendy/channels/coding/output.png"  // optional
    }

    Filename format: {channel_id}_{timestamp_ns}.json

Message Log:
    Sent messages are logged to message_log.jsonl for debugging and
    correlation between outbox timestamps and Discord message IDs.

Architecture:
    Proxy API -> outbox/ files -> WendyOutbox.watch_outbox -> Discord
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands, tasks

from .paths import DB_PATH as DEFAULT_DB_PATH
from .paths import OUTBOX_DIR as DEFAULT_OUTBOX_DIR
from .paths import SHARED_DIR, WENDY_BASE

_LOG = logging.getLogger(__name__)

# Database path - allow override via env var
_env_db_path = os.getenv("WENDY_DB_PATH")
DB_PATH: Path = Path(_env_db_path) if _env_db_path else DEFAULT_DB_PATH

# =============================================================================
# Configuration Constants
# =============================================================================

_env_outbox_dir = os.getenv("WENDY_OUTBOX_DIR")
OUTBOX_DIR: Path = Path(_env_outbox_dir) if _env_outbox_dir else DEFAULT_OUTBOX_DIR
"""Directory to watch for outgoing message files."""

MESSAGE_LOG_FILE: Path = WENDY_BASE / "message_log.jsonl"
"""JSONL file for logging sent messages (for debugging)."""

MAX_MESSAGE_LOG_LINES: int = 1000
"""Maximum lines to keep in message log before trimming."""

MAX_FILE_SIZE_MB: int = 25
"""Maximum attachment size in MB (Discord's default limit for non-boosted servers)."""

EMOJI_CACHE_FILE: Path = SHARED_DIR / "emojis.json"
"""JSON file where guild custom emojis are cached for proxy lookups."""


class WendyOutbox(commands.Cog):
    """Discord cog that watches the outbox directory and sends queued messages.

    This cog polls the OUTBOX_DIR every 0.5 seconds for JSON files. When found,
    it parses the message data, sends it to the specified Discord channel,
    logs the correlation, and deletes the file.

    Attributes:
        bot: The Discord bot instance.
    """

    def __init__(self, bot: commands.Bot) -> None:
        """Initialize the outbox watcher and start the polling task.

        Args:
            bot: The Discord bot instance.
        """
        self.bot = bot
        self._ensure_outbox_dir()
        self.watch_outbox.start()
        self.refresh_emoji_cache.start()
        _LOG.info("WendyOutbox initialized, watching %s", OUTBOX_DIR)

    def _ensure_outbox_dir(self) -> None:
        """Create the outbox directory if it doesn't exist."""
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

    def _cache_sent_message(self, sent_msg: discord.Message) -> None:
        """Cache Wendy's sent message to the database.

        This ensures Wendy's own messages appear in her conversation history,
        since on_message filters out bot messages before caching.

        Args:
            sent_msg: The Discord message object returned from channel.send().
        """
        try:
            conn = sqlite3.connect(DB_PATH)

            # Store in message_history table (unified with message_logger cog)
            attachment_urls = ",".join(a.url for a in sent_msg.attachments) if sent_msg.attachments else None
            conn.execute("""
                INSERT OR REPLACE INTO message_history
                (message_id, channel_id, guild_id, author_id, author_nickname,
                 is_bot, content, timestamp, attachment_urls)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sent_msg.id,
                sent_msg.channel.id,
                sent_msg.guild.id if sent_msg.guild else None,
                sent_msg.author.id,
                sent_msg.author.display_name,
                1,  # is_bot = True for Wendy's own messages
                sent_msg.content,
                sent_msg.created_at.isoformat(),
                attachment_urls,
            ))

            conn.commit()
            conn.close()
            _LOG.debug("Cached Wendy's sent message %d to database", sent_msg.id)
        except Exception as e:
            _LOG.error("Failed to cache sent message: %s", e)

    def _log_sent_message(
        self,
        discord_msg_id: int,
        outbox_ts: int,
        channel_id: int,
        content: str
    ) -> None:
        """Log a sent message to message_log.jsonl for debug correlation.

        Args:
            discord_msg_id: The Discord snowflake ID of the sent message.
            outbox_ts: The nanosecond timestamp from the outbox filename.
            channel_id: The Discord channel ID.
            content: The message content (truncated in log).
        """
        try:
            MESSAGE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

            log_entry = {
                "discord_msg_id": discord_msg_id,
                "outbox_ts": outbox_ts,
                "channel_id": channel_id,
                "content_preview": content[:100] if content else "",
            }

            with open(MESSAGE_LOG_FILE, "a") as f:
                f.write(json.dumps(log_entry) + "\n")

            _LOG.debug("Logged message correlation: discord=%d, outbox_ts=%d", discord_msg_id, outbox_ts)
            self._trim_message_log_if_needed()

        except Exception as e:
            _LOG.error("Failed to log sent message: %s", e)

    def _trim_message_log_if_needed(self) -> None:
        """Trim message log to MAX_MESSAGE_LOG_LINES if it exceeds the limit.

        Keeps only the most recent lines to prevent unbounded growth.
        """
        try:
            if not MESSAGE_LOG_FILE.exists():
                return

            with open(MESSAGE_LOG_FILE) as f:
                lines = f.readlines()

            if len(lines) > MAX_MESSAGE_LOG_LINES:
                with open(MESSAGE_LOG_FILE, "w") as f:
                    f.writelines(lines[-MAX_MESSAGE_LOG_LINES:])
                _LOG.info("Trimmed message log from %d to %d lines", len(lines), MAX_MESSAGE_LOG_LINES)
        except Exception as e:
            _LOG.error("Failed to trim message log: %s", e)

    def _extract_outbox_timestamp(self, filename: str) -> int | None:
        """Extract the nanosecond timestamp from an outbox filename.

        Args:
            filename: Filename like '123456_1234567890123.json'.

        Returns:
            Timestamp as integer, or None if filename doesn't match pattern.
        """
        match = re.match(r"\d+_(\d+)\.json$", filename)
        if match:
            return int(match.group(1))
        return None

    def cog_unload(self) -> None:
        """Clean up when the cog is unloaded."""
        self.watch_outbox.cancel()
        self.refresh_emoji_cache.cancel()

    @tasks.loop(seconds=0.5)
    async def watch_outbox(self) -> None:
        """Poll the outbox directory and process any JSON files found.

        Runs every 0.5 seconds. Each file is processed independently.
        """
        try:
            for file_path in sorted(OUTBOX_DIR.glob("*.json")):
                await self._process_outbox_file(file_path)
        except Exception as e:
            _LOG.error("Error watching outbox: %s", e)

    @watch_outbox.before_loop
    async def before_watch(self) -> None:
        """Wait for bot to be ready before starting the watcher."""
        await self.bot.wait_until_ready()

    def _build_attachment(self, file_path_str: str | None, channel: discord.TextChannel) -> discord.File | None:
        """Build a discord.File from a file path string.

        Validates file existence and size before creating the attachment.

        Args:
            file_path_str: Filesystem path to the attachment, or None.
            channel: Discord channel (used for error reporting).

        Returns:
            discord.File if valid, None otherwise.
        """
        if not file_path_str:
            return None

        attachment_path = Path(file_path_str)
        if not attachment_path.exists():
            _LOG.warning("Attachment file not found: %s", file_path_str)
            return None

        file_size_mb = attachment_path.stat().st_size / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            _LOG.error("File too large: %s is %.1fMB (limit %dMB)", attachment_path.name, file_size_mb, MAX_FILE_SIZE_MB)
            return None

        _LOG.info("Attaching file: %s (%.1fMB)", attachment_path, file_size_mb)
        return discord.File(attachment_path)

    async def _process_outbox_file(self, outbox_file: Path) -> None:
        """Process a single outbox file and send the message to Discord.

        Dispatches to batch or single-message processing based on the presence
        of an 'actions' key in the JSON data.

        Files are always deleted after processing (even on error) to prevent
        infinite retry loops.

        Args:
            outbox_file: Path to the outbox JSON file.
        """
        try:
            data = json.loads(outbox_file.read_text())
            channel_id = int(data["channel_id"])

            channel = self.bot.get_channel(channel_id)
            if not channel:
                # Thread channels may not be cached - try fetching from API
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden):
                    _LOG.warning("Channel %s not found or inaccessible, skipping message", channel_id)
                    outbox_file.unlink()
                    return
                except discord.HTTPException as e:
                    _LOG.warning("Failed to fetch channel %s: %s, skipping message", channel_id, e)
                    outbox_file.unlink()
                    return

            if "actions" in data:
                await self._process_actions(channel, data["actions"], outbox_file.name)
            else:
                await self._process_single_message(channel, data, outbox_file.name)

            outbox_file.unlink()

        except json.JSONDecodeError as e:
            _LOG.error("Invalid JSON in outbox file %s: %s", outbox_file, e)
            outbox_file.unlink()
        except Exception as e:
            _LOG.error("Error processing outbox file %s: %s", outbox_file, e)
            outbox_file.unlink()

    async def _process_single_message(
        self, channel: discord.TextChannel, data: dict, filename: str
    ) -> None:
        """Process a single-message outbox entry.

        Args:
            channel: Discord channel to send to.
            data: Parsed outbox JSON data.
            filename: Outbox filename (for logging).
        """
        message_text = data.get("message") or data.get("content") or ""
        file_path_str = data.get("file_path")
        attachment = self._build_attachment(file_path_str, channel)

        # Build reply reference if reply_to is specified
        reference = None
        reply_to = data.get("reply_to")
        if reply_to:
            try:
                reference = discord.MessageReference(
                    message_id=int(reply_to), channel_id=channel.id
                )
            except (ValueError, TypeError):
                _LOG.warning("Invalid reply_to value: %s", reply_to)

        # Guard: skip if there is nothing to send
        if not message_text and not attachment:
            _LOG.warning("Skipping empty outbox message (no text or attachment) for channel %s, file %s", channel.id, filename)
            return

        # Send the message
        kwargs = {}
        if attachment:
            kwargs["file"] = attachment
        if reference:
            kwargs["reference"] = reference
            kwargs["mention_author"] = False

        sent_msg = await channel.send(message_text, **kwargs)

        _LOG.info("Sent Wendy outbox message to channel %s (msg_id=%s): %s...",
                 channel.id, sent_msg.id, message_text[:50])

        self._cache_sent_message(sent_msg)

        outbox_ts = self._extract_outbox_timestamp(filename)
        if outbox_ts:
            self._log_sent_message(
                discord_msg_id=sent_msg.id,
                outbox_ts=outbox_ts,
                channel_id=channel.id,
                content=message_text,
            )

    async def _process_actions(
        self, channel: discord.TextChannel, actions: list[dict], filename: str
    ) -> None:
        """Process a batch of actions from an outbox file.

        Each action is processed independently - errors in one action don't
        block subsequent actions.

        Args:
            channel: Discord channel to operate in.
            actions: List of action dicts from outbox JSON.
            filename: Outbox filename (for logging).
        """
        for i, action in enumerate(actions):
            try:
                action_type = action.get("type")

                if action_type == "send_message":
                    text = action.get("content", "")
                    att_path = action.get("file_path") or action.get("attachment")
                    attachment = self._build_attachment(att_path, channel)

                    # Guard: skip if there is nothing to send
                    if not text and not attachment:
                        _LOG.warning("Batch action %d: skipping empty send_message (no text or attachment)", i)
                        continue

                    reference = None
                    reply_to = action.get("reply_to")
                    if reply_to:
                        try:
                            reference = discord.MessageReference(
                                message_id=int(reply_to), channel_id=channel.id
                            )
                        except (ValueError, TypeError):
                            _LOG.warning("Invalid reply_to in action %d: %s", i, reply_to)

                    kwargs = {}
                    if attachment:
                        kwargs["file"] = attachment
                    if reference:
                        kwargs["reference"] = reference
                        kwargs["mention_author"] = False

                    sent_msg = await channel.send(text, **kwargs)
                    _LOG.info("Batch action %d: sent message %s", i, sent_msg.id)
                    self._cache_sent_message(sent_msg)

                    outbox_ts = self._extract_outbox_timestamp(filename)
                    if outbox_ts:
                        self._log_sent_message(
                            discord_msg_id=sent_msg.id,
                            outbox_ts=outbox_ts,
                            channel_id=channel.id,
                            content=text,
                        )

                elif action_type == "add_reaction":
                    message_id = action.get("message_id")
                    emoji = action.get("emoji")
                    if not message_id or not emoji:
                        _LOG.warning("Batch action %d: add_reaction missing message_id or emoji", i)
                        continue

                    target_msg = await channel.fetch_message(int(message_id))
                    await target_msg.add_reaction(emoji)
                    _LOG.info("Batch action %d: reacted with %s on message %s", i, emoji, message_id)

                else:
                    _LOG.warning("Batch action %d: unknown type '%s'", i, action_type)

            except Exception as e:
                _LOG.error("Batch action %d failed: %s", i, e)

    @tasks.loop(minutes=15)
    async def refresh_emoji_cache(self) -> None:
        """Cache guild custom emojis to a JSON file for proxy lookups."""
        try:
            all_emojis = []
            for guild in self.bot.guilds:
                for emoji in guild.emojis:
                    prefix = "a" if emoji.animated else ""
                    all_emojis.append({
                        "name": emoji.name,
                        "id": str(emoji.id),
                        "animated": emoji.animated,
                        "guild": guild.name,
                        "usage": f"<{prefix}:{emoji.name}:{emoji.id}>",
                    })

            EMOJI_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            EMOJI_CACHE_FILE.write_text(json.dumps(all_emojis))
            _LOG.debug("Refreshed emoji cache: %d emojis", len(all_emojis))

        except Exception as e:
            _LOG.error("Failed to refresh emoji cache: %s", e)

    @refresh_emoji_cache.before_loop
    async def before_refresh_emoji_cache(self) -> None:
        """Wait for bot to be ready before starting the emoji cache task."""
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension setup function.

    Called by bot.load_extension() to register the cog.

    Args:
        bot: The Discord bot instance.
    """
    await bot.add_cog(WendyOutbox(bot))
