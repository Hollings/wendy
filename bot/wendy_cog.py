"""WendyCog - Main Discord cog for Wendy bot.

This module implements the Discord.py cog that:
- Listens for messages in whitelisted channels
- Caches incoming messages to SQLite for context building
- Downloads and saves attachments locally
- Triggers Claude CLI sessions in response to messages
- Monitors for orchestrator task completions and wakes Wendy
- Provides !context and !reset commands for session management

Architecture:
    Discord Gateway -> WendyCog.on_message -> ClaudeCliTextGenerator -> Proxy API

The cog maintains per-channel state:
- Active generation jobs (prevents concurrent Claude CLI instances)
- Channel configurations (folder, mode, permissions)
- Message cache in SQLite

Configuration is via environment variables:
    WENDY_CHANNEL_CONFIG: JSON array of channel configs
    WENDY_WHITELIST_CHANNELS: Comma-separated channel IDs (legacy)
    WENDY_DB_PATH: Path to SQLite database
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands, tasks

from .claude_cli import ClaudeCliError, ClaudeCliTextGenerator

_LOG = logging.getLogger(__name__)

# =============================================================================
# Configuration Constants
# =============================================================================

DB_PATH: Path = Path(os.getenv("WENDY_DB_PATH", "/data/wendy.db"))
"""Path to SQLite database for caching Discord messages."""

ATTACHMENTS_DIR: Path = Path("/data/wendy/attachments")
"""Directory for storing downloaded message attachments."""

TASK_COMPLETIONS_FILE: Path = Path("/data/wendy/task_completions.json")
"""JSON file where orchestrator writes task completion notifications."""


class GenerationJob:
    """Tracks an active Claude CLI generation for a channel.

    Used to prevent concurrent generations in the same channel and to
    allow cleanup when the generation completes.

    Attributes:
        task: The asyncio Task running the generation, or None if not started.
    """

    def __init__(self) -> None:
        """Initialize an empty generation job."""
        self.task: asyncio.Task | None = None


class WendyCog(commands.Cog):
    """Main Discord cog for Wendy bot.

    Handles message listening, caching, attachment downloads, and
    coordinating with the Claude CLI for response generation.

    Attributes:
        bot: The Discord bot instance.
        generator: ClaudeCliTextGenerator for running Claude sessions.
        channel_configs: Map of channel_id to channel configuration dicts.
        whitelist_channels: Set of channel IDs where Wendy listens.

    Example channel config format:
        {"id": "123", "name": "coding", "folder": "coding", "mode": "full"}

    Modes:
        - "full": Full coding capabilities with all tools
        - "chat": Limited to conversation without file access
    """

    def __init__(self, bot: commands.Bot) -> None:
        """Initialize the cog with channel configurations.

        Loads channel config from WENDY_CHANNEL_CONFIG (JSON) or falls back
        to WENDY_WHITELIST_CHANNELS (comma-separated IDs).

        Args:
            bot: The Discord bot instance.
        """
        self.bot = bot
        self.generator = ClaudeCliTextGenerator()

        # Channel configuration - maps channel_id to config dict
        # Config format: {"id": "123", "name": "chat", "folder": "chat", "mode": "chat"}
        self.channel_configs: dict[int, dict] = {}
        self.whitelist_channels: set[int] = set()

        # Try new JSON config format first
        config_json = os.getenv("WENDY_CHANNEL_CONFIG", "")
        if config_json:
            try:
                configs = json.loads(config_json)
                for cfg in configs:
                    channel_id = int(cfg["id"])
                    self.channel_configs[channel_id] = cfg
                    self.whitelist_channels.add(channel_id)
                _LOG.info("Loaded %d channel configs from WENDY_CHANNEL_CONFIG", len(self.channel_configs))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                _LOG.error("Failed to parse WENDY_CHANNEL_CONFIG: %s", e)

        # Fallback to old comma-separated format for backwards compatibility
        if not self.whitelist_channels:
            whitelist_str = os.getenv("WENDY_WHITELIST_CHANNELS", "")
            if whitelist_str:
                for cid_str in whitelist_str.split(","):
                    try:
                        channel_id = int(cid_str.strip())
                        self.whitelist_channels.add(channel_id)
                        # Create default config for backwards compatibility
                        self.channel_configs[channel_id] = {
                            "id": str(channel_id),
                            "name": "default",
                            "folder": "wendys_folder",
                            "mode": "full"
                        }
                    except ValueError:
                        pass

        # Active generations (per channel)
        self._active_generations: dict[int, GenerationJob] = {}

        # Initialize database
        self._init_db()

        # Start task completion watcher if we have whitelisted channels
        if self.whitelist_channels:
            self.watch_task_completions.start()
            _LOG.info("Task completion watcher started")

        _LOG.info("WendyCog initialized with %d whitelisted channels", len(self.whitelist_channels))

    def _init_db(self) -> None:
        """Initialize the SQLite database schema for message caching.

        Creates two tables:
        - cached_messages: Recent messages for interrupt detection
        - message_history: Full history with reactions and attachments
        """
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

    async def _save_attachments(self, message: discord.Message) -> list[str]:
        """Download and save message attachments to local filesystem.

        Files are saved with pattern: msg_{message_id}_{index}_{filename}
        This allows the proxy to find attachments by message ID.

        Args:
            message: Discord message with potential attachments.

        Returns:
            List of absolute paths to saved attachment files.
        """
        if not message.attachments:
            return []

        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        paths = []

        for i, attachment in enumerate(message.attachments):
            try:
                # Create filename with message ID for lookup
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
        """Handle incoming Discord messages.

        This is the main event handler. For each message:
        1. Filters out own messages, DMs, commands, empty messages
        2. Checks channel whitelist
        3. Caches the message and downloads attachments
        4. Starts a Claude CLI session if bot should respond

        Only one generation can run per channel at a time.

        Args:
            message: Incoming Discord message.
        """
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

        # Save any attachments
        await self._save_attachments(message)

        # Check if bot was mentioned or should respond
        if not await self._should_respond(message):
            return

        # Use haiku for webhook messages (cheaper for automated triggers)
        is_webhook = message.webhook_id is not None
        if is_webhook:
            _LOG.info("Processing WEBHOOK message from %s: %s...", message.author.display_name, message.content[:50])
        else:
            _LOG.info("Processing message from %s: %s...", message.author.display_name, message.content[:50])

        # Check for existing generation
        existing_job = self._active_generations.get(message.channel.id)
        if existing_job and existing_job.task and not existing_job.task.done():
            # Claude CLI is already running, it will check for new messages
            _LOG.info("Claude CLI already running in channel %s, skipping", message.channel.id)
            return

        # Start generation (use haiku for webhooks to save costs)
        job = GenerationJob()
        model_override = "haiku" if is_webhook else None
        task = self.bot.loop.create_task(self._generate_response(message, job, model_override=model_override))
        job.task = task
        self._active_generations[message.channel.id] = job

    def _channel_allowed(self, message: discord.Message) -> bool:
        """Check if the message's channel is allowed for processing.

        Allows message if:
        - Bot is directly mentioned (any channel), OR
        - Channel is in the whitelist

        Args:
            message: Discord message to check.

        Returns:
            True if the channel is allowed.
        """
        # If bot is mentioned, allow any channel
        if self.bot.user in message.mentions:
            return True

        # Check whitelist
        if not self.whitelist_channels:
            return False
        return message.channel.id in self.whitelist_channels

    async def _should_respond(self, message: discord.Message) -> bool:
        """Determine if bot should actively respond to this message.

        Args:
            message: Discord message to check.

        Returns:
            True if Wendy should generate a response.
        """
        # Always respond if mentioned
        if self.bot.user in message.mentions:
            return True

        # Respond in whitelisted channels
        return message.channel.id in self.whitelist_channels

    async def _generate_response(
        self, message: discord.Message, job: GenerationJob, model_override: str = None
    ) -> None:
        """Generate and send a response using Claude CLI.

        Runs the Claude CLI with the channel's configuration. Handles OAuth
        expiration errors specially by notifying the channel.

        Args:
            message: The triggering Discord message.
            job: GenerationJob tracking this generation.
            model_override: Optional model to use (e.g., "haiku" for webhooks).
        """
        channel = message.channel
        channel_config = self.channel_configs.get(channel.id, {})

        try:
            # Run Claude CLI with channel-specific config
            await self.generator.generate(
                channel_id=channel.id,
                channel_config=channel_config,
                model_override=model_override,
            )
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
        """Show current Claude session statistics for this channel.

        Displays session ID, creation time, message count, and token usage.

        Args:
            ctx: Discord command context.
        """
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
        """Reset the Claude session for this channel.

        Creates a new session, clearing conversation history and context.

        Args:
            ctx: Discord command context.
        """
        channel_id = ctx.channel.id
        new_session_id = self.generator.reset_channel_session(channel_id)
        await ctx.send(f"Session reset. New session: `{new_session_id[:8]}...`")

    @tasks.loop(seconds=5)
    async def watch_task_completions(self) -> None:
        """Watch for orchestrator task completions and wake Wendy.

        Polls TASK_COMPLETIONS_FILE every 5 seconds. When unseen completions
        are found, marks them as seen and triggers a generation in the
        "full" mode channel to let Wendy review the results.
        """
        if not self.whitelist_channels:
            return

        try:
            if not TASK_COMPLETIONS_FILE.exists():
                return

            # Orchestrator writes completions as a list directly
            data = json.loads(TASK_COMPLETIONS_FILE.read_text())
            if isinstance(data, list):
                completions = data
            elif isinstance(data, dict):
                completions = data.get("completions", [])
            else:
                completions = []

            # Find unseen completions
            unseen = [c for c in completions if not c.get("seen_by_wendy", False)]
            if not unseen:
                return

            _LOG.info("Found %d unseen task completions, waking Wendy", len(unseen))

            # Mark all as seen
            for c in completions:
                c["seen_by_wendy"] = True
            TASK_COMPLETIONS_FILE.write_text(json.dumps(completions, indent=2))

            # Use coding channel for task completions (or first channel with full mode)
            channel_id = None
            for cid, cfg in self.channel_configs.items():
                if cfg.get("mode") == "full":
                    channel_id = cid
                    break
            if not channel_id:
                channel_id = next(iter(self.whitelist_channels), None)
            if not channel_id:
                _LOG.warning("No channel available for task completion")
                return
            channel = self.bot.get_channel(channel_id)
            if not channel:
                _LOG.warning("Whitelist channel %d not found", channel_id)
                return

            # Check for existing generation
            existing_job = self._active_generations.get(channel.id)
            if existing_job and existing_job.task and not existing_job.task.done():
                _LOG.info("Claude CLI already running, skipping task wake")
                return

            # Start generation (same as on_message but without a trigger message)
            job = GenerationJob()
            task = self.bot.loop.create_task(self._generate_response_for_channel(channel, job))
            job.task = task
            self._active_generations[channel.id] = job

        except Exception as e:
            _LOG.error("Error watching task completions: %s", e)

    @watch_task_completions.before_loop
    async def before_watch_task_completions(self) -> None:
        """Wait for bot to be ready before starting the watcher."""
        await self.bot.wait_until_ready()

    async def _generate_response_for_channel(
        self, channel: discord.TextChannel, job: GenerationJob
    ) -> None:
        """Generate a response for a channel without a triggering message.

        Used by the task completion watcher to wake Wendy and let her check
        completed tasks. Similar to _generate_response but without a message.

        Args:
            channel: Target Discord channel.
            job: GenerationJob tracking this generation.
        """
        channel_config = self.channel_configs.get(channel.id, {})

        try:
            await self.generator.generate(channel_id=channel.id, channel_config=channel_config)
            _LOG.info("Claude CLI completed for channel %s (task wake)", channel.id)

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


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension setup function.

    Called by bot.load_extension() to register the cog.

    Args:
        bot: The Discord bot instance.
    """
    await bot.add_cog(WendyCog(bot))
