"""Outbox watcher for Wendy's async message sending.

This module implements a file-watching cog that monitors /data/wendy/outbox/
for JSON files and sends them as Discord messages. This allows Claude CLI
(via the proxy API) to queue messages without direct Discord access.

File Format:
    Outbox files are JSON with the following structure:
    {
        "channel_id": "123456789",
        "message": "Hello world",
        "file_path": "/data/wendy/uploads/foo.png"  // optional
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
from pathlib import Path

import discord
from discord.ext import commands, tasks

_LOG = logging.getLogger(__name__)

# =============================================================================
# Configuration Constants
# =============================================================================

OUTBOX_DIR: Path = Path(os.getenv("WENDY_OUTBOX_DIR", "/data/wendy/outbox"))
"""Directory to watch for outgoing message files."""

MESSAGE_LOG_FILE: Path = Path("/data/wendy/message_log.jsonl")
"""JSONL file for logging sent messages (for debugging)."""

MAX_MESSAGE_LOG_LINES: int = 1000
"""Maximum lines to keep in message log before trimming."""

MAX_FILE_SIZE_MB: int = 25
"""Maximum attachment size in MB (Discord's default limit for non-boosted servers)."""


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
        _LOG.info("WendyOutbox initialized, watching %s", OUTBOX_DIR)

    def _ensure_outbox_dir(self) -> None:
        """Create the outbox directory if it doesn't exist."""
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

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

    @tasks.loop(seconds=0.5)
    async def watch_outbox(self) -> None:
        """Poll the outbox directory and process any JSON files found.

        Runs every 0.5 seconds. Each file is processed independently.
        """
        try:
            for file_path in OUTBOX_DIR.glob("*.json"):
                await self._process_outbox_file(file_path)
        except Exception as e:
            _LOG.error("Error watching outbox: %s", e)

    @watch_outbox.before_loop
    async def before_watch(self) -> None:
        """Wait for bot to be ready before starting the watcher."""
        await self.bot.wait_until_ready()

    async def _process_outbox_file(self, outbox_file: Path) -> None:
        """Process a single outbox file and send the message to Discord.

        Parses the JSON, validates the attachment if any, sends to Discord,
        logs the correlation, and deletes the file.

        Files are always deleted after processing (even on error) to prevent
        infinite retry loops.

        Args:
            outbox_file: Path to the outbox JSON file.
        """
        try:
            data = json.loads(outbox_file.read_text())
            channel_id = int(data["channel_id"])
            message_text = data.get("message") or data.get("content") or ""

            channel = self.bot.get_channel(channel_id)
            if not channel:
                _LOG.warning("Channel %s not found, skipping message", channel_id)
                outbox_file.unlink()
                return

            # Check for file attachment
            file_path_str = data.get("file_path")
            attachment = None
            if file_path_str:
                attachment_path = Path(file_path_str)
                if attachment_path.exists():
                    # Check file size before attempting to send
                    file_size_mb = attachment_path.stat().st_size / (1024 * 1024)
                    if file_size_mb > MAX_FILE_SIZE_MB:
                        error_msg = f"File too large to send: {attachment_path.name} is {file_size_mb:.1f}MB (Discord limit is {MAX_FILE_SIZE_MB}MB)"
                        _LOG.error(error_msg)
                        await channel.send(f"[Outbox error] {error_msg}")
                        outbox_file.unlink()
                        return
                    attachment = discord.File(attachment_path)
                    _LOG.info("Attaching file: %s (%.1fMB)", attachment_path, file_size_mb)
                else:
                    _LOG.warning("Attachment file not found: %s", file_path_str)

            # Send the message
            if attachment:
                sent_msg = await channel.send(message_text, file=attachment)
            else:
                sent_msg = await channel.send(message_text)

            _LOG.info("Sent Wendy outbox message to channel %s (msg_id=%s): %s...",
                     channel_id, sent_msg.id, message_text[:50])

            # Log the message correlation
            outbox_ts = self._extract_outbox_timestamp(outbox_file.name)
            if outbox_ts:
                self._log_sent_message(
                    discord_msg_id=sent_msg.id,
                    outbox_ts=outbox_ts,
                    channel_id=channel_id,
                    content=message_text,
                )

            outbox_file.unlink()

        except json.JSONDecodeError as e:
            _LOG.error("Invalid JSON in outbox file %s: %s", outbox_file, e)
            outbox_file.unlink()
        except Exception as e:
            _LOG.error("Error processing outbox file %s: %s", outbox_file, e)
            # Delete file to prevent infinite retry loop
            outbox_file.unlink()


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension setup function.

    Called by bot.load_extension() to register the cog.

    Args:
        bot: The Discord bot instance.
    """
    await bot.add_cog(WendyOutbox(bot))
