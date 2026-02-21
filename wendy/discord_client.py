"""Discord gateway -- on_message, send_to_channel, attachment saving.

Replaces v1's WendyCog + WendyOutbox with direct discord.py calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

import discord
from discord.ext import commands, tasks

from . import api_server, sessions
from .cli import ClaudeCliError, run_cli, setup_wendy_scripts
from .config import MESSAGE_LOGGER_GUILDS, PROXY_PORT, parse_channel_configs
from .paths import (
    DB_PATH,
    attachments_dir,
    claude_md_path,
    ensure_channel_dirs,
    ensure_shared_dirs,
    session_dir,
)
from .state import state as state_manager
from .tasks import TaskRunner

_LOG = logging.getLogger(__name__)

# Counter for unique synthetic message IDs
_synthetic_counter = 0


class GenerationJob:
    """Tracks an active Claude CLI generation for a channel."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.new_message_pending: bool = False


class WendyBot(commands.Bot):
    """Main Wendy Discord bot.

    Handles message listening, caching, attachment downloads, and
    coordinating with Claude CLI for response generation. Also runs
    the internal HTTP server that Claude CLI curls.
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)

        self.channel_configs: dict[int, dict] = parse_channel_configs()
        self.whitelist_channels: set[int] = set(self.channel_configs.keys())
        self._active_generations: dict[int, GenerationJob] = {}
        self._api_runner = None
        self._presence_updated_at: float = 0

        # Ensure shared directories
        ensure_shared_dirs()

        _LOG.info("WendyBot initialized with %d channels", len(self.whitelist_channels))

        @self.command(name="version")
        async def cmd_version(ctx: commands.Context) -> None:
            """!version -- show the running git commit."""
            import subprocess
            try:
                sha = subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd="/app", stderr=subprocess.DEVNULL,
                ).decode().strip()
                msg = subprocess.check_output(
                    ["git", "log", "-1", "--format=%s"],
                    cwd="/app", stderr=subprocess.DEVNULL,
                ).decode().strip()
                await ctx.send(f"`{sha}` {msg}")
            except Exception:
                await ctx.send("version unknown")

        @self.command(name="system")
        async def cmd_system(ctx: commands.Context) -> None:
            """!system -- upload the assembled system prompt as a text file."""
            import io
            channel_id = ctx.channel.id
            channel_config = self.channel_configs.get(channel_id)
            if channel_config is None:
                await ctx.send("no config for this channel")
                return
            try:
                from .prompt import build_system_prompt
                prompt = build_system_prompt(channel_id, channel_config)
                buf = io.BytesIO(prompt.encode("utf-8"))
                await ctx.send(file=discord.File(buf, filename="system_prompt.txt"))
            except Exception as e:
                await ctx.send(f"error: {e}")

        @self.command(name="clear")
        async def cmd_clear(ctx: commands.Context) -> None:
            """!clear -- reset the current Claude session."""
            channel_id = ctx.channel.id
            channel_config = self.channel_configs.get(channel_id)
            if channel_config is None:
                await ctx.send("not a configured channel")
                return
            folder = channel_config.get("_folder") or channel_config.get("name", "default")
            old_id, new_id = sessions.reset_session(channel_id, folder)
            if old_id:
                await ctx.send(f"session cleared. old: `{old_id[:8]}` new: `{new_id[:8]}`")
            else:
                await ctx.send(f"new session started: `{new_id[:8]}`")

        @self.command(name="resume")
        async def cmd_resume(ctx: commands.Context, *, session_id_prefix: str = "") -> None:
            """!resume <session_id> -- resume a previous session by ID or prefix."""
            if not session_id_prefix:
                await ctx.send("usage: `!resume <session_id>`")
                return
            channel_id = ctx.channel.id
            channel_config = self.channel_configs.get(channel_id)
            if channel_config is None:
                await ctx.send("not a configured channel")
                return
            folder = channel_config.get("_folder") or channel_config.get("name", "default")
            row = state_manager.get_session_by_id(session_id_prefix)
            if not row:
                await ctx.send("session not found")
                return
            full_id = row["session_id"]
            sess_file = session_dir(folder) / f"{full_id}.jsonl"
            if not sess_file.exists():
                await ctx.send(
                    f"warning: session file not found for `{full_id[:8]}`, resuming anyway"
                )
            sessions.resume_session(channel_id, full_id, folder)
            await ctx.send(f"resumed session `{full_id[:8]}`")

        @self.command(name="session")
        async def cmd_session(ctx: commands.Context) -> None:
            """!session -- show current session info."""
            import datetime
            channel_id = ctx.channel.id
            sess = sessions.get_session(channel_id)
            if not sess:
                await ctx.send("no active session")
                return
            started = datetime.datetime.fromtimestamp(
                sess.created_at, tz=datetime.timezone.utc
            )
            started_str = started.strftime("%Y-%m-%d %H:%M UTC")
            total_tokens = sess.total_input_tokens + sess.total_output_tokens
            total_in_with_cache = sess.total_input_tokens + sess.total_cache_read_tokens
            if total_in_with_cache:
                cache_rate = f"{sess.total_cache_read_tokens / total_in_with_cache:.0%}"
            else:
                cache_rate = "n/a"
            lines = [
                f"session: `{sess.session_id[:8]}`",
                f"started: {started_str}",
                f"turns: {sess.message_count}",
                f"tokens: {total_tokens:,} (cache hit: {cache_rate})",
            ]
            await ctx.send("\n".join(lines))

    async def setup_hook(self) -> None:
        """Called when the bot is starting up (before on_ready)."""
        # Setup scripts and fragments
        setup_wendy_scripts()

        # Fragment seeding (imported here to avoid circular imports at module level)
        try:
            from .fragment_setup import setup_fragments_dir
            setup_fragments_dir()
        except ImportError:
            _LOG.info("fragment_setup not available yet, skipping seeding")

        # Set discord bot reference in api_server
        api_server.set_discord_bot(self)
        api_server.set_channel_configs(self.channel_configs)

        # Start API server
        port = int(PROXY_PORT)
        self._api_runner = await api_server.start_server(port)

        # Start notification watcher
        if self.whitelist_channels:
            self.watch_notifications.start()

        # Cache emoji list for the API
        self._cache_emojis_task = self.loop.create_task(self._cache_emojis())

        # Start background task runner (beads)
        self._task_runner = TaskRunner()
        self.loop.create_task(self._task_runner.run())

    async def close(self) -> None:
        """Cleanup on shutdown."""
        if self._api_runner:
            await self._api_runner.cleanup()
        await super().close()

    async def on_ready(self) -> None:
        _LOG.info("Logged in as %s (id=%d)", self.user.name, self.user.id)
        from . import config as _config
        _config.WENDY_BOT_ID = self.user.id

        # Ensure channel directories exist
        for cfg in self.channel_configs.values():
            folder = cfg.get("_folder", cfg.get("name", "default"))
            beads = cfg.get("beads_enabled", False)
            ensure_channel_dirs(folder, beads_enabled=beads)
            from .cli import setup_channel_folder
            setup_channel_folder(folder, beads_enabled=beads)

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming Discord messages."""
        # Ignore own messages
        if message.author.id == self.user.id:
            return

        # Ignore DMs
        if not message.guild:
            return

        # Log ALL messages from whitelisted guilds (guild-wide logging)
        if MESSAGE_LOGGER_GUILDS and message.guild.id in MESSAGE_LOGGER_GUILDS:
            self._cache_message(message)

        # Check channel whitelist
        if not self._channel_allowed(message):
            return

        # Resolve thread config if needed
        if message.channel.id not in self.channel_configs:
            thread_config = self._resolve_thread_config(message)
            if thread_config:
                self.channel_configs[message.channel.id] = thread_config
                api_server.set_channel_configs(self.channel_configs)
                self._setup_thread_directory(thread_config)

        channel_config = self.channel_configs.get(message.channel.id, {})
        channel_name = channel_config.get("_folder") or channel_config.get("name", "default")

        # Ignore commands
        if message.content.startswith(("!", "-", "/")):
            # Process bot commands
            await self.process_commands(message)
            return

        # Ignore empty messages without attachments
        if not message.content.strip() and not message.attachments:
            return

        # Cache to SQLite (if not already logged by guild-wide logging above)
        if not MESSAGE_LOGGER_GUILDS or message.guild.id not in MESSAGE_LOGGER_GUILDS:
            self._cache_message(message)

        # Save attachments
        await self._save_attachments(message, channel_name)

        # Check if bot should respond
        if not self._channel_allowed(message):
            return

        _LOG.info("Processing message from %s: %s...", message.author.display_name, message.content[:50])

        # Check for interrupt trigger ("WENDY" in all caps)
        existing_job = self._active_generations.get(message.channel.id)
        if message.content.strip() == "WENDY":
            if existing_job and existing_job.task and not existing_job.task.done():
                self._interrupt_channel(message, existing_job, channel_config, channel_name)
                return

        # Check for existing generation
        if existing_job and existing_job.task and not existing_job.task.done():
            existing_job.new_message_pending = True
            _LOG.info("CLI already running in channel %s, marked pending", message.channel.id)
            return

        # Start generation
        model_override = channel_config.get("model")
        job = GenerationJob()
        task = self.loop.create_task(self._generate_response(message.channel, job, model_override=model_override))
        job.task = task
        self._active_generations[message.channel.id] = job

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Update cached message content when edited."""
        if not payload.guild_id:
            return
        in_logger_guild = bool(MESSAGE_LOGGER_GUILDS and payload.guild_id in MESSAGE_LOGGER_GUILDS)
        in_whitelisted_channel = payload.channel_id in self.whitelist_channels
        if not in_logger_guild and not in_whitelisted_channel:
            return
        data = payload.data
        if "content" not in data:
            return
        try:
            state_manager.update_message_content(payload.message_id, data["content"])
        except Exception as e:
            _LOG.error("Failed to update edited message %s: %s", payload.message_id, e)

    def _channel_allowed(self, message: discord.Message) -> bool:
        if self.user in message.mentions:
            return True
        if message.channel.id in self.whitelist_channels:
            return True
        if isinstance(message.channel, discord.Thread):
            return message.channel.parent_id in self.whitelist_channels
        return False

    def _cache_message(self, message: discord.Message) -> None:
        """Cache a Discord message to SQLite."""
        attachment_urls = None
        if message.attachments:
            attachment_urls = json.dumps([a.url for a in message.attachments])

        reply_to_id = None
        if message.reference and message.reference.message_id:
            reply_to_id = message.reference.message_id

        state_manager.insert_message(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild else None,
            author_id=message.author.id,
            author_nickname=message.author.display_name,
            is_bot=message.author.bot,
            content=message.content,
            timestamp=int(message.created_at.timestamp()),
            attachment_urls=attachment_urls,
            reply_to_id=reply_to_id,
            is_webhook=bool(message.webhook_id),
        )

    async def _save_attachments(self, message: discord.Message, channel_name: str) -> list[str]:
        """Download and save message attachments."""
        if not message.attachments:
            return []

        att_dir = attachments_dir(channel_name)
        att_dir.mkdir(parents=True, exist_ok=True)
        paths = []

        for i, attachment in enumerate(message.attachments):
            try:
                filename = f"msg_{message.id}_{i}_{attachment.filename}"
                filepath = att_dir / filename
                data = await attachment.read()
                filepath.write_bytes(data)
                paths.append(str(filepath))
                _LOG.info("Saved attachment: %s (%d bytes)", filepath, len(data))
            except Exception as e:
                _LOG.error("Failed to save attachment %s: %s", attachment.filename, e)

        # Verify files exist (handles filesystem flush delays)
        for path_str in paths:
            path = Path(path_str)
            for _attempt in range(3):
                if path.exists():
                    break
                await asyncio.sleep(0.1)

        return paths

    def _resolve_thread_config(self, message: discord.Message) -> dict | None:
        """Resolve thread configuration from parent channel."""
        if not isinstance(message.channel, discord.Thread):
            return None

        parent_id = message.channel.parent_id
        parent_config = self.channel_configs.get(parent_id)
        if not parent_config:
            return None

        parent_folder = parent_config.get("_folder") or parent_config.get("name", "default")
        thread_id = message.channel.id
        folder_name = f"{parent_folder}_t_{thread_id}"
        thread_name = message.channel.name or "unknown-thread"

        config = {
            "id": str(thread_id),
            "name": thread_name,
            "mode": parent_config.get("mode", "chat"),
            "model": parent_config.get("model"),
            "beads_enabled": parent_config.get("beads_enabled", False),
            "_folder": folder_name,
            "_is_thread": True,
            "_parent_folder": parent_folder,
            "_parent_channel_id": parent_id,
            "_thread_name": thread_name,
        }

        state_manager.register_thread(thread_id, parent_id, folder_name, thread_name)
        _LOG.info("Resolved thread config: thread=%d parent=%d folder=%s", thread_id, parent_id, folder_name)
        return config

    def _setup_thread_directory(self, thread_config: dict) -> None:
        """Set up workspace directory for a thread."""
        from .paths import channel_dir

        folder_name = thread_config["_folder"]
        parent_folder = thread_config["_parent_folder"]
        beads_enabled = thread_config.get("beads_enabled", False)

        thread_dir = channel_dir(folder_name)
        is_new = not thread_dir.exists()

        ensure_channel_dirs(folder_name, beads_enabled=beads_enabled)

        if is_new:
            parent_md = claude_md_path(parent_folder)
            thread_md = claude_md_path(folder_name)
            if parent_md.exists() and not thread_md.exists():
                shutil.copy2(parent_md, thread_md)

    def _build_system_prompt(self, channel_id: int, channel_config: dict) -> str:
        """Build system prompt via prompt.py."""
        from .prompt import build_system_prompt
        return build_system_prompt(channel_id, channel_config)

    def _interrupt_channel(
        self,
        message: discord.Message,
        existing_job: GenerationJob,
        channel_config: dict,
        channel_name: str,
    ) -> None:
        """Cancel the running generation, reset session, and start a fresh one."""
        channel_id = message.channel.id
        model_override = channel_config.get("model")

        # Replace the active generation BEFORE cancelling so the old job's finally
        # block sees a different job and won't start its own restart.
        new_job = GenerationJob()
        self._active_generations[channel_id] = new_job

        # Cancel the old task (CancelledError propagates into run_cli which kills the subprocess)
        existing_job.task.cancel()
        _LOG.info("Interrupted active generation for channel %s by %s", channel_id, message.author.display_name)

        # Insert synthetic notice so Wendy sees it on check_messages
        self._insert_synthetic_message(
            channel_id,
            "System",
            f"[{message.author.display_name} interrupted you. Whatever you were doing may not be finished.]",
        )

        new_task = self.loop.create_task(
            self._generate_response(message.channel, new_job, model_override=model_override)
        )
        new_job.task = new_task

    async def _maybe_update_presence(self) -> None:
        """Update Discord presence with usage stats if stale (>15 min)."""
        import time
        if time.monotonic() - self._presence_updated_at < 900:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "/app/scripts/get_usage.sh",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            data = json.loads(stdout.decode())
            week_pct = data.get("week_all_percent", 0)
            session_pct = data.get("session_percent", 0)
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{week_pct}% weekly | {session_pct}% session",
                )
            )
            self._presence_updated_at = time.monotonic()
        except Exception as e:
            _LOG.error("Failed to update presence: %s", e)

    async def _generate_response(
        self,
        channel: discord.TextChannel | discord.Thread,
        job: GenerationJob,
        model_override: str | None = None,
    ) -> None:
        """Generate a response using Claude CLI."""
        channel_config = self.channel_configs.get(channel.id, {})

        await self._maybe_update_presence()

        try:
            system_prompt = self._build_system_prompt(channel.id, channel_config)
            await run_cli(
                channel_id=channel.id,
                channel_config=channel_config,
                system_prompt=system_prompt,
                model_override=model_override,
            )
            _LOG.info("CLI completed for channel %s", channel.id)

        except ClaudeCliError as e:
            error_str = str(e).lower()
            if "oauth" in error_str and "expired" in error_str:
                try:
                    await channel.send(
                        "my claude cli token expired - someone needs to run "
                        "`docker exec -it wendy claude login` to fix me"
                    )
                except Exception:
                    _LOG.exception("Failed to send OAuth expiration notice")
            else:
                _LOG.error("Claude CLI error: %s", e)

        except Exception:
            _LOG.exception("Generation failed")

        finally:
            if self._active_generations.get(channel.id) is job:
                if job.new_message_pending and self._has_pending_messages(channel.id):
                    _LOG.info("New messages pending in channel %s, starting new generation", channel.id)
                    new_job = GenerationJob()
                    new_task = self.loop.create_task(
                        self._generate_response(channel, new_job)
                    )
                    new_job.task = new_task
                    self._active_generations[channel.id] = new_job
                else:
                    self._active_generations.pop(channel.id, None)

    def _has_pending_messages(self, channel_id: int) -> bool:
        """Check if there are unread messages for a channel."""
        try:
            last_seen = state_manager.get_last_seen(channel_id)
            if not DB_PATH.exists():
                return False

            import sqlite3
            conn = sqlite3.connect(str(DB_PATH))
            try:
                wendy_bot_id = self.user.id
                if last_seen:
                    count = conn.execute(
                        """
                        SELECT COUNT(*) FROM message_history
                        WHERE channel_id = ? AND message_id > ?
                        AND author_id != ?
                        AND content NOT LIKE '!%' AND content NOT LIKE '-%'
                        """,
                        (channel_id, last_seen, wendy_bot_id)
                    ).fetchone()[0]
                else:
                    count = conn.execute(
                        """
                        SELECT COUNT(*) FROM message_history
                        WHERE channel_id = ? AND author_id != ?
                        AND content NOT LIKE '!%' AND content NOT LIKE '-%'
                        LIMIT 1
                        """,
                        (channel_id, wendy_bot_id)
                    ).fetchone()[0]
                return count > 0
            finally:
                conn.close()
        except Exception as e:
            _LOG.error("Error checking pending messages: %s", e)
            return True  # Fail open

    @tasks.loop(seconds=5)
    async def watch_notifications(self) -> None:
        """Watch for notifications and wake Wendy."""
        if not self.whitelist_channels:
            return

        try:
            unseen = state_manager.get_unseen_notifications_for_wendy()
            if not unseen:
                return

            _LOG.info("Found %d unseen notifications", len(unseen))

            channels_to_wake = set()
            notification_ids = []

            for notif in unseen:
                notification_ids.append(notif.id)

                if notif.type == "task_completion":
                    channel_id = notif.channel_id
                    if not channel_id:
                        for cid, cfg in self.channel_configs.items():
                            if cfg.get("mode") == "full":
                                channel_id = cid
                                break
                        if not channel_id:
                            channel_id = next(iter(self.whitelist_channels), None)

                    if channel_id:
                        payload = notif.payload or {}
                        task_id = payload.get("task_id", "unknown")
                        status = payload.get("status", "completed")
                        duration = payload.get("duration", "")

                        author = "Task System"
                        content = f"[{author}] Background task {task_id} ({notif.title}) {status}"
                        if duration:
                            content += f" in {duration}"
                        content += ". YOU MUST send a message to the channel announcing this completion."

                        self._insert_synthetic_message(channel_id, author, content)
                        channels_to_wake.add(channel_id)

                elif notif.type == "webhook":
                    channel_id = notif.channel_id
                    if not channel_id or channel_id not in self.whitelist_channels:
                        continue

                    author = f"Webhook: {notif.source.title()}"
                    payload_content = ""
                    if notif.payload:
                        raw_data = notif.payload.get("raw", notif.payload)
                        if isinstance(raw_data, dict):
                            payload_content = "\n" + json.dumps(raw_data, indent=2)
                        elif raw_data:
                            payload_content = f"\n{raw_data}"
                    content = f"[{author}] {notif.title}{payload_content}"

                    self._insert_synthetic_message(channel_id, author, content)
                    # Don't add to channels_to_wake -- webhooks queue but don't wake Wendy

            if notification_ids:
                state_manager.mark_notifications_seen_by_wendy(notification_ids)

            for channel_id in channels_to_wake:
                channel = self.get_channel(channel_id)
                if not channel:
                    continue

                existing_job = self._active_generations.get(channel_id)
                if existing_job and existing_job.task and not existing_job.task.done():
                    existing_job.new_message_pending = True
                    continue

                job = GenerationJob()
                task = self.loop.create_task(self._generate_response(channel, job))
                job.task = task
                self._active_generations[channel_id] = job

        except Exception as e:
            _LOG.error("Error watching notifications: %s", e)

    @watch_notifications.before_loop
    async def before_watch_notifications(self) -> None:
        await self.wait_until_ready()

    def _insert_synthetic_message(
        self, channel_id: int, author: str, content: str, guild_id: int | None = None,
    ) -> None:
        """Insert a synthetic message for one-time notification delivery."""
        global _synthetic_counter
        import time
        _synthetic_counter += 1
        synthetic_id = 9_000_000_000_000_000_000 + int(time.time_ns() // 1000) + _synthetic_counter
        state_manager.insert_message(
            message_id=synthetic_id,
            channel_id=channel_id,
            guild_id=guild_id,
            author_id=0,
            author_nickname=author,
            is_bot=False,
            content=content,
            timestamp=int(time.time()),
        )

    async def _cache_emojis(self) -> None:
        """Cache guild emojis to JSON for the API endpoint."""
        await self.wait_until_ready()
        try:
            all_emojis = []
            for guild in self.guilds:
                for emoji in guild.emojis:
                    all_emojis.append({
                        "name": emoji.name,
                        "id": str(emoji.id),
                        "animated": emoji.animated,
                        "usage": f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>",
                    })

            from .paths import SHARED_DIR
            emoji_file = SHARED_DIR / "emojis.json"
            emoji_file.write_text(json.dumps(all_emojis))
            _LOG.info("Cached %d emojis", len(all_emojis))
        except Exception as e:
            _LOG.error("Failed to cache emojis: %s", e)
