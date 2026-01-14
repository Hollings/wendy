"""WendyCog - Main Discord cog for Wendy bot."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from .claude_cli import ClaudeCliTextGenerator, ClaudeCliError

_LOG = logging.getLogger(__name__)

# Database for caching messages
DB_PATH = Path(os.getenv("WENDY_DB_PATH", "/data/wendy.db"))
ATTACHMENTS_DIR = Path("/data/wendy/attachments")


class GenerationJob:
    """Tracks active generation state."""

    def __init__(self):
        self.task: asyncio.Task | None = None


class WendyCog(commands.Cog):
    """Main Wendy Discord bot cog."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.generator = ClaudeCliTextGenerator()

        # Channel whitelist
        whitelist_str = os.getenv("WENDY_WHITELIST_CHANNELS", "")
        self.whitelist_channels: set[int] = set()
        if whitelist_str:
            for cid_str in whitelist_str.split(","):
                try:
                    self.whitelist_channels.add(int(cid_str.strip()))
                except ValueError:
                    pass

        # Active generations (per channel)
        self._active_generations: dict[int, GenerationJob] = {}

        # Initialize database
        self._init_db()

        _LOG.info("WendyCog initialized with %d whitelisted channels", len(self.whitelist_channels))

    def _init_db(self) -> None:
        """Initialize the SQLite database for message caching."""
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cached_messages (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                has_images INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cached_messages_channel
            ON cached_messages(channel_id, message_id DESC)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_history (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                author_nickname TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                reactions TEXT,
                attachment_urls TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_history_channel
            ON message_history(channel_id, message_id DESC)
        """)
        conn.commit()
        conn.close()
        _LOG.info("Database initialized at %s", DB_PATH)

    def _cache_message(self, message: discord.Message) -> None:
        """Cache a Discord message to the database."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                INSERT OR REPLACE INTO cached_messages
                (message_id, channel_id, author_id, author_name, content, timestamp, has_images)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                message.id,
                message.channel.id,
                message.author.id,
                message.author.display_name,
                message.content,
                int(message.created_at.timestamp()),
                1 if message.attachments else 0,
            ))

            # Also store in message_history for full history queries
            attachment_urls = ",".join(a.url for a in message.attachments) if message.attachments else None
            conn.execute("""
                INSERT OR REPLACE INTO message_history
                (message_id, channel_id, author_nickname, content, timestamp, attachment_urls)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                message.id,
                message.channel.id,
                message.author.display_name,
                message.content,
                int(message.created_at.timestamp()),
                attachment_urls,
            ))

            conn.commit()
            conn.close()
        except Exception as e:
            _LOG.error("Failed to cache message: %s", e)

    async def _save_attachments(self, message: discord.Message) -> list[str]:
        """Download and save message attachments, return paths."""
        if not message.attachments:
            return []

        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        paths = []

        for i, attachment in enumerate(message.attachments):
            try:
                # Create filename with message ID for lookup
                ext = Path(attachment.filename).suffix or ".bin"
                filename = f"msg_{message.id}_{i}_{attachment.filename}"
                filepath = ATTACHMENTS_DIR / filename

                # Download and save
                data = await attachment.read()
                filepath.write_bytes(data)
                paths.append(str(filepath))
                _LOG.info("Saved attachment: %s (%d bytes)", filepath, len(data))
            except Exception as e:
                _LOG.error("Failed to save attachment %s: %s", attachment.filename, e)

        return paths

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages."""
        # Ignore own messages
        if message.author.id == self.bot.user.id:
            return

        # Ignore DMs
        if not message.guild:
            return

        # Check channel whitelist
        if not self._channel_allowed(message):
            return

        # Ignore commands
        if message.content.startswith(("!", "-", "/")):
            return

        # Ignore empty messages without attachments
        if not message.content.strip() and not message.attachments:
            return

        # Cache the message
        self._cache_message(message)

        # Save any attachments
        await self._save_attachments(message)

        # Check if bot was mentioned or should respond
        if not await self._should_respond(message):
            return

        _LOG.info("Processing message from %s: %s...", message.author.display_name, message.content[:50])

        # Check for existing generation
        existing_job = self._active_generations.get(message.channel.id)
        if existing_job and existing_job.task and not existing_job.task.done():
            # Claude CLI is already running, it will check for new messages
            _LOG.info("Claude CLI already running in channel %s, skipping", message.channel.id)
            return

        # Start generation
        job = GenerationJob()
        task = self.bot.loop.create_task(self._generate_response(message, job))
        job.task = task
        self._active_generations[message.channel.id] = job

    def _channel_allowed(self, message: discord.Message) -> bool:
        """Check if channel is in whitelist."""
        # If bot is mentioned, allow any channel
        if self.bot.user in message.mentions:
            return True

        # Check whitelist
        if not self.whitelist_channels:
            return False
        return message.channel.id in self.whitelist_channels

    async def _should_respond(self, message: discord.Message) -> bool:
        """Determine if bot should respond to this message."""
        # Always respond if mentioned
        if self.bot.user in message.mentions:
            return True

        # Respond in whitelisted channels
        return message.channel.id in self.whitelist_channels

    async def _generate_response(self, message: discord.Message, job: GenerationJob) -> None:
        """Generate a response using Claude CLI."""
        channel = message.channel

        try:
            # Run Claude CLI
            await self.generator.generate(channel_id=channel.id)
            _LOG.info("Claude CLI completed for channel %s", channel.id)

        except ClaudeCliError as e:
            error_str = str(e).lower()
            if "oauth" in error_str and "expired" in error_str:
                try:
                    await channel.send(
                        "my claude cli token expired - someone needs to run "
                        "`docker exec -it wendy-bot claude login` to fix me"
                    )
                except Exception:
                    _LOG.exception("Failed to send OAuth expiration notice")
            else:
                _LOG.error("Claude CLI error: %s", e)

        except Exception as e:
            _LOG.exception("Generation failed: %s", e)

        finally:
            if self._active_generations.get(channel.id) is job:
                self._active_generations.pop(channel.id, None)

    @commands.command(name="context")
    async def context_command(self, ctx: commands.Context) -> None:
        """Show session stats for this channel."""
        channel_id = ctx.channel.id
        stats = self.generator.get_session_stats(channel_id)

        if not stats:
            await ctx.send("No active session for this channel.")
            return

        from datetime import datetime
        created_at = datetime.fromtimestamp(stats.get("created_at", 0))
        last_used = stats.get("last_used_at")
        last_used_str = datetime.fromtimestamp(last_used).strftime("%H:%M:%S") if last_used else "never"

        msg = f"""**Session Stats**
Session: `{stats.get('session_id', 'unknown')[:8]}...`
Created: {created_at.strftime("%Y-%m-%d %H:%M:%S")}
Last used: {last_used_str}
Messages: {stats.get('message_count', 0)}

**Token Usage (cumulative)**
Input: {stats.get('total_input_tokens', 0):,}
Output: {stats.get('total_output_tokens', 0):,}
Cache read: {stats.get('total_cache_read_tokens', 0):,}
Cache create: {stats.get('total_cache_create_tokens', 0):,}
"""
        await ctx.send(msg)

    @commands.command(name="reset")
    async def reset_command(self, ctx: commands.Context) -> None:
        """Reset the channel's session."""
        channel_id = ctx.channel.id
        new_session_id = self.generator.reset_channel_session(channel_id)
        await ctx.send(f"Session reset. New session: `{new_session_id[:8]}...`")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WendyCog(bot))
