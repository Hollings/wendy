"""WendyCog - Main Discord cog for Wendy bot.

This module implements the Discord.py cog that:
- Listens for messages in whitelisted channels
- Caches incoming messages to SQLite for context building
- Downloads and saves attachments locally
- Triggers Claude CLI sessions in response to messages
- Monitors for notifications (task completions, webhooks) and wakes Wendy
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

import discord
from discord.ext import commands, tasks

from .claude_cli import ClaudeCliError, ClaudeCliTextGenerator
from .paths import (
    DB_PATH,
    attachments_dir,
    ensure_shared_dirs,
    validate_channel_name,
)
from .state_manager import state as state_manager

_LOG = logging.getLogger(__name__)


class GenerationJob:
    """Tracks an active Claude CLI generation for a channel.

    Used to prevent concurrent generations in the same channel and to
    allow cleanup when the generation completes.

    Attributes:
        task: The asyncio Task running the generation, or None if not started.
        new_message_pending: True if new messages arrived while this job was running.
    """

    def __init__(self) -> None:
        """Initialize an empty generation job."""
        self.task: asyncio.Task | None = None
        self.new_message_pending: bool = False


class WendyCog(commands.Cog):
    """Main Discord cog for Wendy bot.

    Handles message listening, caching, attachment downloads, and
    coordinating with the Claude CLI for response generation.

    Attributes:
        bot: The Discord bot instance.
        generator: ClaudeCliTextGenerator for running Claude sessions.
        channel_configs: Map of channel_id to channel configuration dicts.
        whitelist_channels: Set of channel IDs where Wendy listens.

    Example channel config format (new):
        {"id": "123", "name": "coding", "mode": "full", "beads_enabled": true}

    Note: The 'name' field is used as the folder name. No separate 'folder' field.

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
        # Config format: {"id": "123", "name": "coding", "mode": "full", "beads_enabled": true}
        self.channel_configs: dict[int, dict] = {}
        self.whitelist_channels: set[int] = set()

        # Try new JSON config format first
        config_json = os.getenv("WENDY_CHANNEL_CONFIG", "")
        if config_json:
            try:
                configs = json.loads(config_json)
                for cfg in configs:
                    parsed = self._parse_channel_config(cfg)
                    if parsed:
                        channel_id = int(parsed["id"])
                        self.channel_configs[channel_id] = parsed
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
                            "mode": "full",
                            "beads_enabled": False,
                        }
                    except ValueError:
                        pass

        # Active generations (per channel)
        self._active_generations: dict[int, GenerationJob] = {}

        # Ensure shared directories exist
        ensure_shared_dirs()

        # Initialize database
        self._init_db()

        # Start notification watcher if we have whitelisted channels
        if self.whitelist_channels:
            self.watch_notifications.start()
            _LOG.info("Notifications watcher started")

        _LOG.info("WendyCog initialized with %d whitelisted channels", len(self.whitelist_channels))

    def _parse_channel_config(self, cfg: dict) -> dict | None:
        """Parse and validate a channel configuration.

        Args:
            cfg: Raw channel config dict from JSON.

        Returns:
            Normalized config dict, or None if invalid.
        """
        # Required fields
        if "id" not in cfg:
            _LOG.error("Channel config missing 'id' field: %s", cfg)
            return None
        if "name" not in cfg:
            _LOG.error("Channel config missing 'name' field: %s", cfg)
            return None

        name = cfg["name"]
        if not validate_channel_name(name):
            _LOG.error(
                "Invalid channel name '%s' - must match ^[a-zA-Z0-9_-]+$",
                name
            )
            return None

        # Build normalized config
        # Support legacy 'folder' field by using it if present, otherwise use 'name'
        folder = cfg.get("folder", name)
        if not validate_channel_name(folder):
            _LOG.warning(
                "Invalid folder '%s' in config, using name '%s' instead",
                folder, name
            )
            folder = name

        return {
            "id": str(cfg["id"]),
            "name": name,
            "mode": cfg.get("mode", "chat"),
            "model": cfg.get("model"),  # None means use default
            "beads_enabled": cfg.get("beads_enabled", False),
            # Internal: actual folder to use (supports legacy 'folder' field)
            "_folder": folder,
        }

    def _init_db(self) -> None:
        """Initialize the SQLite database schema.

        Note: message_history table is created by the message_logger cog.
        This method ensures the database directory exists and creates any
        wendy-specific tables if needed.
        """
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOG.info("Database path verified at %s", DB_PATH)

    async def _save_attachments(self, message: discord.Message, channel_name: str) -> list[str]:
        """Download and save message attachments to local filesystem.

        Files are saved with pattern: msg_{message_id}_{index}_{filename}
        This allows the proxy to find attachments by message ID.

        Attachments are stored per-channel to ensure Claude sessions in one
        channel cannot access attachments from other channels.

        Args:
            message: Discord message with potential attachments.
            channel_name: Name of the channel (used as folder name).

        Returns:
            List of absolute paths to saved attachment files.
        """
        if not message.attachments:
            return []

        att_dir = attachments_dir(channel_name)
        att_dir.mkdir(parents=True, exist_ok=True)
        paths = []

        for i, attachment in enumerate(message.attachments):
            try:
                # Create filename with message ID for lookup
                filename = f"msg_{message.id}_{i}_{attachment.filename}"
                filepath = att_dir / filename

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

        # Check channel whitelist and get config
        if not self._channel_allowed(message):
            return
        channel_config = self.channel_configs.get(message.channel.id, {})
        channel_name = channel_config.get("_folder") or channel_config.get("name", "default")

        # Ignore commands
        if message.content.startswith(("!", "-", "/")):
            return

        # Ignore empty messages without attachments
        if not message.content.strip() and not message.attachments:
            return

        # ALWAYS save attachments, even if we skip generation
        # This prevents race conditions where check_messages sees a message
        # but the attachment file hasn't been downloaded yet
        await self._save_attachments(message, channel_name)

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
            # Claude CLI is already running - mark that new messages arrived
            # so we can start a new generation when the current one finishes
            existing_job.new_message_pending = True
            _LOG.info("Claude CLI already running in channel %s, marked pending", message.channel.id)
            return

        # Determine model: webhooks use haiku, otherwise use channel config model
        if is_webhook:
            model_override = "haiku"
        else:
            model_override = channel_config.get("model")  # None means use default

        # Start generation
        job = GenerationJob()
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
                # Check if new messages arrived while we were running
                if job.new_message_pending:
                    # Verify there are actually messages to process before starting
                    # a new generation - synthetic messages may have already been
                    # consumed by our check_messages call
                    if self._has_pending_messages(channel.id):
                        _LOG.info("New messages pending in channel %s, starting new generation", channel.id)
                        # Start a new generation for the pending messages
                        new_job = GenerationJob()
                        new_task = self.bot.loop.create_task(
                            self._generate_response_for_channel(channel, new_job)
                        )
                        new_job.task = new_task
                        self._active_generations[channel.id] = new_job
                    else:
                        _LOG.info("new_message_pending was True but no messages found, skipping generation")
                        self._active_generations.pop(channel.id, None)
                else:
                    self._active_generations.pop(channel.id, None)

    def _has_pending_messages(self, channel_id: int) -> bool:
        """Check if there are pending messages for a channel.

        Used to verify that a new generation is warranted before starting one.
        Returns True if there are unread messages (real or synthetic) in the DB.
        """
        import sqlite3

        from bot.paths import DB_PATH

        if not DB_PATH.exists():
            return False

        try:
            last_seen = state_manager.get_last_seen(channel_id)
            conn = sqlite3.connect(str(DB_PATH))
            try:
                wendy_bot_id = self.bot.user.id
                if last_seen:
                    query = """
                        SELECT COUNT(*) FROM message_history
                        WHERE channel_id = ? AND message_id > ?
                        AND author_id != ?
                        AND content NOT LIKE '!%'
                        AND content NOT LIKE '-%'
                    """
                    count = conn.execute(query, (channel_id, last_seen, wendy_bot_id)).fetchone()[0]
                else:
                    query = """
                        SELECT COUNT(*) FROM message_history
                        WHERE channel_id = ?
                        AND author_id != ?
                        AND content NOT LIKE '!%'
                        AND content NOT LIKE '-%'
                        LIMIT 1
                    """
                    count = conn.execute(query, (channel_id, wendy_bot_id)).fetchone()[0]
                return count > 0
            finally:
                conn.close()
        except Exception as e:
            _LOG.error("Error checking pending messages: %s", e)
            return True  # Fail open - better to wake than miss messages

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
    async def watch_notifications(self) -> None:
        """Watch for notifications (task completions, webhooks) and wake Wendy.

        Polls SQLite every 5 seconds. When unseen notifications are found:
        - task_completion: Just wake Wendy in the "full" mode channel
        - webhook: Insert synthetic message, then wake Wendy in target channel
        """
        if not self.whitelist_channels:
            return

        try:
            # Get unseen notifications from SQLite
            unseen = state_manager.get_unseen_notifications_for_wendy()
            if not unseen:
                return

            _LOG.info("Found %d unseen notifications", len(unseen))

            # Get message logger cog for inserting synthetic messages (webhooks)
            message_logger = self.bot.get_cog("MessageLoggerCog")

            # Track channels to wake and notification IDs to mark as seen
            channels_to_wake = set()
            notification_ids = []

            for notif in unseen:
                notification_ids.append(notif.id)

                if notif.type == "task_completion":
                    # Task completions go to the "full" mode channel
                    channel_id = notif.channel_id
                    if not channel_id:
                        # Find default "full" mode channel
                        for cid, cfg in self.channel_configs.items():
                            if cfg.get("mode") == "full":
                                channel_id = cid
                                break
                        if not channel_id:
                            channel_id = next(iter(self.whitelist_channels), None)

                    if channel_id:
                        # Insert synthetic message so Wendy knows to announce the completion
                        payload = notif.payload or {}
                        task_id = payload.get("task_id", "unknown")
                        status = payload.get("status", "completed")
                        duration = payload.get("duration", "")

                        author = "Task System"
                        content = f"[{author}] Background task {task_id} ({notif.title}) {status}"
                        if duration:
                            content += f" in {duration}"
                        content += ". YOU MUST send a message to the channel announcing this completion - this is a required system notification, not optional."

                        if message_logger:
                            channel_config = self.channel_configs.get(channel_id, {})
                            guild_id = channel_config.get("guild_id")
                            if guild_id:
                                try:
                                    guild_id = int(guild_id)
                                except ValueError:
                                    guild_id = None

                            message_logger.insert_synthetic_message(
                                channel_id=channel_id,
                                author_nickname=author,
                                content=content,
                                guild_id=guild_id,
                            )

                        channels_to_wake.add(channel_id)

                elif notif.type == "webhook":
                    channel_id = notif.channel_id
                    if not channel_id:
                        _LOG.warning("Webhook notification without channel_id: %s", notif.title)
                        continue

                    # Check if this channel is in our whitelist
                    if channel_id not in self.whitelist_channels:
                        _LOG.warning("Webhook for non-whitelisted channel %d", channel_id)
                        continue

                    # Insert synthetic message so Claude can see the webhook content
                    author = f"Webhook: {notif.source.title()}"
                    # Include payload content if available
                    payload_content = ""
                    if notif.payload:
                        # Extract the raw webhook data
                        raw_data = notif.payload.get("raw", notif.payload)
                        if isinstance(raw_data, dict):
                            # Format dict as readable content
                            payload_content = "\n" + json.dumps(raw_data, indent=2)
                        elif raw_data:
                            payload_content = f"\n{raw_data}"
                    content = f"[{author}] {notif.title}{payload_content}"

                    if message_logger:
                        # Get guild_id from channel config if available
                        channel_config = self.channel_configs.get(channel_id, {})
                        guild_id = channel_config.get("guild_id")
                        if guild_id:
                            try:
                                guild_id = int(guild_id)
                            except ValueError:
                                guild_id = None

                        message_logger.insert_synthetic_message(
                            channel_id=channel_id,
                            author_nickname=author,
                            content=content,
                            guild_id=guild_id,
                        )
                    else:
                        _LOG.warning("MessageLoggerCog not found, cannot insert synthetic message")

                    channels_to_wake.add(channel_id)

                else:
                    _LOG.warning("Unknown notification type: %s", notif.type)

            # Mark all notifications as seen
            if notification_ids:
                state_manager.mark_notifications_seen_by_wendy(notification_ids)

            # Wake Wendy in each affected channel
            for channel_id in channels_to_wake:
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    _LOG.warning("Channel %d not found", channel_id)
                    continue

                # Check for existing generation
                existing_job = self._active_generations.get(channel_id)
                if existing_job and existing_job.task and not existing_job.task.done():
                    # Mark that new messages arrived so we'll process after current gen
                    existing_job.new_message_pending = True
                    _LOG.info("Claude CLI already running in channel %d, marked pending", channel_id)
                    continue

                # Start generation
                job = GenerationJob()
                task = self.bot.loop.create_task(
                    self._generate_response_for_channel(channel, job)
                )
                job.task = task
                self._active_generations[channel_id] = job
                _LOG.info("Triggered generation for notification in channel %d", channel_id)

        except Exception as e:
            _LOG.error("Error watching notifications: %s", e)

    @watch_notifications.before_loop
    async def before_watch_notifications(self) -> None:
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
        model_override = channel_config.get("model")

        try:
            await self.generator.generate(
                channel_id=channel.id,
                channel_config=channel_config,
                model_override=model_override,
            )
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
                # Check if new messages arrived while we were running
                if job.new_message_pending:
                    # Verify there are actually messages to process before starting
                    # a new generation - synthetic messages may have already been
                    # consumed by our check_messages call
                    if self._has_pending_messages(channel.id):
                        _LOG.info("New messages pending in channel %s, starting new generation", channel.id)
                        new_job = GenerationJob()
                        new_task = self.bot.loop.create_task(
                            self._generate_response_for_channel(channel, new_job)
                        )
                        new_job.task = new_task
                        self._active_generations[channel.id] = new_job
                    else:
                        _LOG.info("new_message_pending was True but no messages found, skipping generation")
                        self._active_generations.pop(channel.id, None)
                else:
                    self._active_generations.pop(channel.id, None)


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension setup function.

    Called by bot.load_extension() to register the cog.

    Args:
        bot: The Discord bot instance.
    """
    await bot.add_cog(WendyCog(bot))
