"""Discord gateway -- message handling, attachment saving, CLI orchestration.

Bridges Discord events to Claude CLI sessions. Each whitelisted channel gets
a persistent Claude CLI session; incoming messages trigger CLI invocations
that respond via the internal HTTP API.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import discord
from discord.ext import commands, tasks

from . import api_server, sessions
from .cli import ClaudeCliError, run_cli, setup_wendy_scripts
from .config import (
    ENRICHMENT_DURATION,
    ENRICHMENT_HOUR_UTC,
    ENRICHMENT_MINUTE_UTC,
    FEATURE_DIGEST_CHANNEL,
    FEATURE_DIGEST_HOUR_UTC,
    MESSAGE_LOGGER_GUILDS,
    PROXY_PORT,
    USAGE_BUDGET_FACTOR,
    parse_channel_configs,
)
from .enrichment import build_enrichment_continue_nudge, build_enrichment_end_nudge, build_enrichment_nudge
from .paths import (
    attachments_dir,
    claude_md_path,
    ensure_channel_dirs,
    ensure_shared_dirs,
    session_dir,
)
from .state import state as state_manager
from .tasks import TaskRunner

_LOG = logging.getLogger(__name__)

_synthetic_counter = 0
"""Counter for unique synthetic message IDs."""

_cached_usage: dict = {}
"""Latest parsed usage data from get_usage.sh (updated by _maybe_update_presence)."""

# Minimum seconds between presence updates (15 minutes).
_PRESENCE_INTERVAL = 900


def _folder_for_config(config: dict) -> str:
    """Return the workspace folder name for a channel or thread config."""
    return config.get("_folder") or config.get("name", "default")


def _get_current_effort(model: str) -> list[str]:
    """Return ``["--effort", "low"]`` for Opus when weekly usage >= 85%, else ``[]``.

    The ``--effort`` flag only affects Opus; Sonnet/Haiku ignore it, so we
    return an empty list for non-Opus models unconditionally.
    """
    if "opus" not in model:
        return []
    week_pct = _cached_usage.get("week_all_percent", 0)
    if week_pct >= 85:
        return ["--effort", "low"]
    return []


_MAX_TIMEOUT_CONTINUATIONS = 2
"""Max times a timed-out generation will auto-continue before giving up."""

_MAX_ENRICHMENT_CONTINUATIONS = 10
"""Max times an enrichment session will re-invoke before giving up."""


class GenerationJob:
    """Tracks an active Claude CLI generation for a channel."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.new_message_pending: bool = False
        self.is_enrichment: bool = False
        self.enrichment_end_time: str = ""
        self.enrichment_end_timestamp: float = 0.0
        self.enrichment_continuation: bool = False
        self.enrichment_continuation_count: int = 0
        self.timed_out: bool = False
        self.continuation_count: int = 0
        self.overload_retried: bool = False


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
        self._presence_updated_at: float = 0.0
        self._enrichment_last_run_date: dict[int, datetime.date] = {}
        self._enrichment_notified: set[int] = set()
        self._feature_digest_last_date: datetime.date | None = None
        self._pending_wakes: dict[int, asyncio.TimerHandle] = {}

        ensure_shared_dirs()
        self._register_commands()

        _LOG.info("WendyBot initialized with %d channels", len(self.whitelist_channels))

    # ------------------------------------------------------------------
    # Bot commands (!version, !system, !clear, !resume, !session)
    # ------------------------------------------------------------------

    def _register_commands(self) -> None:
        """Register all ``!`` prefixed bot commands."""

        @self.command(name="version")
        async def cmd_version(ctx: commands.Context) -> None:
            """!version -- show the running git commit."""
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
            channel_config = self.channel_configs.get(ctx.channel.id)
            if channel_config is None:
                await ctx.send("no config for this channel")
                return
            try:
                from .prompt import build_system_prompt
                prompt = build_system_prompt(ctx.channel.id, channel_config)
                buf = io.BytesIO(prompt.encode("utf-8"))
                await ctx.send(file=discord.File(buf, filename="system_prompt.txt"))
            except Exception as e:
                await ctx.send(f"error: {e}")

        @self.command(name="clear")
        async def cmd_clear(ctx: commands.Context) -> None:
            """!clear -- reset the current Claude session."""
            channel_config = self.channel_configs.get(ctx.channel.id)
            if channel_config is None:
                await ctx.send("not a configured channel")
                return
            folder = _folder_for_config(channel_config)
            old_id, new_id = sessions.reset_session(ctx.channel.id, folder)
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
            channel_config = self.channel_configs.get(ctx.channel.id)
            if channel_config is None:
                await ctx.send("not a configured channel")
                return
            folder = _folder_for_config(channel_config)
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
            sessions.resume_session(ctx.channel.id, full_id, folder)
            await ctx.send(f"resumed session `{full_id[:8]}`")

        @self.command(name="lunchtime")
        async def cmd_lunchtime(ctx: commands.Context) -> None:
            """!lunchtime -- start Wendy's personal free-time session."""
            channel_config = self.channel_configs.get(ctx.channel.id)
            if channel_config is None:
                await ctx.send("not a configured channel")
                return
            existing_job = self._active_generations.get(ctx.channel.id)
            if self._job_is_running(existing_job) and existing_job.is_enrichment:
                await ctx.send("already on lunch break!")
                return
            if self._job_is_running(existing_job):
                existing_job.task.cancel()
            self._start_enrichment(ctx.channel, channel_config, manual=True)

        @self.command(name="endlunch")
        async def cmd_endlunch(ctx: commands.Context) -> None:
            """!endlunch -- end Wendy's lunch break early."""
            existing_job = self._active_generations.get(ctx.channel.id)
            if not (self._job_is_running(existing_job) and existing_job.is_enrichment):
                await ctx.send("no lunch break active")
                return
            # Set end timestamp to past so _finalize_generation doesn't re-invoke.
            existing_job.enrichment_end_timestamp = 0.0
            self._enrichment_notified.discard(ctx.channel.id)
            existing_job.task.cancel()
            await ctx.send("lunch break ended")

        @self.command(name="session")
        async def cmd_session(ctx: commands.Context) -> None:
            """!session -- show current session info."""
            sess = sessions.get_session(ctx.channel.id)
            if not sess:
                await ctx.send("no active session")
                return
            started = datetime.datetime.fromtimestamp(
                sess.created_at, tz=datetime.UTC
            )
            started_str = started.strftime("%Y-%m-%d %H:%M UTC")
            total_tokens = sess.total_input_tokens + sess.total_output_tokens
            total_in_with_cache = sess.total_input_tokens + sess.total_cache_read_tokens
            cache_rate = (
                f"{sess.total_cache_read_tokens / total_in_with_cache:.0%}"
                if total_in_with_cache else "n/a"
            )
            lines = [
                f"session: `{sess.session_id[:8]}`",
                f"started: {started_str}",
                f"turns: {sess.message_count}",
                f"tokens: {total_tokens:,} (cache hit: {cache_rate})",
            ]
            await ctx.send("\n".join(lines))

    async def setup_hook(self) -> None:
        """Pre-ready initialization: scripts, API server, background loops."""
        setup_wendy_scripts()

        try:
            from .fragment_setup import setup_fragments_dir
            setup_fragments_dir()
        except ImportError:
            _LOG.info("fragment_setup not available yet, skipping seeding")

        api_server.set_discord_bot(self)
        api_server.set_channel_configs(self.channel_configs)
        self._api_runner = await api_server.start_server(int(PROXY_PORT))

        if self.whitelist_channels:
            self.watch_notifications.start()
            self.check_enrichment_schedule.start()
            self.send_feature_digest.start()

        self._cache_emojis_task = self.loop.create_task(self._cache_emojis())
        self._task_runner = TaskRunner()
        self._task_runner_task = self.loop.create_task(self._task_runner.run())

    async def close(self) -> None:
        """Cleanup on shutdown: cancel the task runner so agents are killed cleanly."""
        if hasattr(self, "_task_runner_task") and not self._task_runner_task.done():
            self._task_runner_task.cancel()
            try:
                await self._task_runner_task
            except asyncio.CancelledError:
                pass
        if self._api_runner:
            await self._api_runner.cleanup()
        await super().close()

    async def on_ready(self) -> None:
        """Publish bot ID and ensure workspace directories for every channel."""
        _LOG.info("Logged in as %s (id=%d)", self.user.name, self.user.id)
        from . import config as _config
        _config.WENDY_BOT_ID = self.user.id

        from .cli import setup_channel_folder
        for cfg in self.channel_configs.values():
            folder = _folder_for_config(cfg)
            beads = cfg.get("beads_enabled", False)
            ensure_channel_dirs(folder, beads_enabled=beads)
            setup_channel_folder(folder, beads_enabled=beads)

    async def on_message(self, message: discord.Message) -> None:
        """Route an incoming Discord message to caching, commands, or CLI generation."""
        if message.author.id == self.user.id or not message.guild:
            return

        # Guild-wide message logging (before any whitelist filtering).
        if MESSAGE_LOGGER_GUILDS and message.guild.id in MESSAGE_LOGGER_GUILDS:
            self._cache_message(message)

        if not self._channel_allowed(message):
            return

        self._ensure_thread_config(message)

        channel_config = self.channel_configs.get(message.channel.id, {})
        channel_name = _folder_for_config(channel_config)

        # Bot commands (!, -, /) are dispatched and not forwarded to CLI.
        if message.content.startswith(("!", "-", "/")):
            await self.process_commands(message)
            return

        if not message.content.strip() and not message.attachments:
            return

        # Cache to SQLite if not already logged by guild-wide logging above.
        if not MESSAGE_LOGGER_GUILDS or message.guild.id not in MESSAGE_LOGGER_GUILDS:
            self._cache_message(message)

        await self._save_attachments(message, channel_name)

        # Ignored users: message is stored but does not wake the bot.
        if message.author.id in channel_config.get("ignore_user_ids", set()):
            return

        _LOG.info("Processing message from %s: %s...", message.author.display_name, message.content[:50])

        # Interrupt: "WENDY" in all caps cancels the running generation.
        existing_job = self._active_generations.get(message.channel.id)
        if message.content.strip() == "WENDY" and self._job_is_running(existing_job):
            self._interrupt_channel(message, existing_job, channel_config)
            return

        # If a generation is already running: enrich sessions suppress new messages,
        # normal sessions flag pending so a follow-up runs when the CLI finishes.
        if self._job_is_running(existing_job):
            if existing_job.is_enrichment:
                if message.channel.id not in self._enrichment_notified:
                    self._enrichment_notified.add(message.channel.id)
                    end_time = existing_job.enrichment_end_time
                    asyncio.ensure_future(
                        message.channel.send(
                            f"<@{message.author.id}> Wendy's on her lunch break until {end_time} UTC! She'll be back soon."
                        )
                    )
                return
            existing_job.new_message_pending = True
            _LOG.info("CLI already running in channel %s, marked pending", message.channel.id)
            return

        self._start_generation(message.channel, channel_config)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Propagate message edits to SQLite so check_messages sees fresh content."""
        if not payload.guild_id:
            return
        in_logger_guild = bool(MESSAGE_LOGGER_GUILDS and payload.guild_id in MESSAGE_LOGGER_GUILDS)
        if not in_logger_guild and payload.channel_id not in self.whitelist_channels:
            return
        if "content" not in payload.data:
            return
        try:
            state_manager.update_message_content(payload.message_id, payload.data["content"])
        except Exception as e:
            _LOG.error("Failed to update edited message %s: %s", payload.message_id, e)

    # ------------------------------------------------------------------
    # Channel / thread helpers
    # ------------------------------------------------------------------

    def _channel_allowed(self, message: discord.Message) -> bool:
        """Return True if the message is from a whitelisted channel or mentions the bot."""
        if self.user in message.mentions:
            return True
        if message.channel.id in self.whitelist_channels:
            return True
        if isinstance(message.channel, discord.Thread):
            return message.channel.parent_id in self.whitelist_channels
        return False

    def _ensure_thread_config(self, message: discord.Message) -> None:
        """Lazily create a channel config entry for new threads."""
        if message.channel.id in self.channel_configs:
            return
        thread_config = self._resolve_thread_config(message)
        if thread_config:
            self.channel_configs[message.channel.id] = thread_config
            api_server.set_channel_configs(self.channel_configs)
            self._setup_thread_directory(thread_config)

    @staticmethod
    def _resolve_mentions(message: discord.Message) -> str:
        """Replace <@USER_ID> mention tokens with @display_name (id) in message content."""
        content = message.content
        for member in message.mentions:
            display = member.display_name
            replacement = f"@{display} (id:{member.id})"
            content = content.replace(f"<@{member.id}>", replacement)
            content = content.replace(f"<@!{member.id}>", replacement)
        return content

    def _cache_message(self, message: discord.Message) -> None:
        """Persist a Discord message to SQLite for later retrieval by check_messages."""
        attachment_urls = (
            json.dumps([a.url for a in message.attachments])
            if message.attachments else None
        )
        reply_to_id = (
            message.reference.message_id
            if message.reference and message.reference.message_id else None
        )

        state_manager.insert_message(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild else None,
            author_id=message.author.id,
            author_nickname=message.author.display_name,
            is_bot=message.author.bot,
            content=self._resolve_mentions(message),
            timestamp=int(message.created_at.timestamp()),
            attachment_urls=attachment_urls,
            reply_to_id=reply_to_id,
            is_webhook=bool(message.webhook_id),
        )

    async def _save_attachments(self, message: discord.Message, channel_name: str) -> list[str]:
        """Download message attachments to the channel's attachments directory.

        Returns a list of saved file paths. Retries existence checks to handle
        filesystem flush delays on network mounts.
        """
        if not message.attachments:
            return []

        att_dir = attachments_dir(channel_name)
        att_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []

        for i, attachment in enumerate(message.attachments):
            try:
                filepath = att_dir / f"msg_{message.id}_{i}_{attachment.filename}"
                data = await attachment.read()
                filepath.write_bytes(data)
                saved.append(str(filepath))
                _LOG.info("Saved attachment: %s (%d bytes)", filepath, len(data))
            except Exception as e:
                _LOG.error("Failed to save attachment %s: %s", attachment.filename, e)

        for path_str in saved:
            p = Path(path_str)
            for _ in range(3):
                if p.exists():
                    break
                await asyncio.sleep(0.1)

        return saved

    def _resolve_thread_config(self, message: discord.Message) -> dict | None:
        """Build a channel config dict for a thread, inheriting from its parent.

        Returns ``None`` if the message is not in a thread or the parent channel
        is not whitelisted.
        """
        if not isinstance(message.channel, discord.Thread):
            return None

        parent_config = self.channel_configs.get(message.channel.parent_id)
        if not parent_config:
            return None

        parent_folder = _folder_for_config(parent_config)
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
            "_parent_channel_id": message.channel.parent_id,
            "_thread_name": thread_name,
        }

        state_manager.register_thread(thread_id, message.channel.parent_id, folder_name, thread_name)
        _LOG.info("Resolved thread config: thread=%d parent=%d folder=%s",
                   thread_id, message.channel.parent_id, folder_name)
        return config

    def _setup_thread_directory(self, thread_config: dict) -> None:
        """Create workspace dirs for a thread, copying the parent CLAUDE.md if new."""
        from .paths import channel_dir

        folder_name = thread_config["_folder"]
        thread_dir = channel_dir(folder_name)
        is_new = not thread_dir.exists()

        ensure_channel_dirs(folder_name, beads_enabled=thread_config.get("beads_enabled", False))

        if is_new:
            parent_md = claude_md_path(thread_config["_parent_folder"])
            thread_md = claude_md_path(folder_name)
            if parent_md.exists() and not thread_md.exists():
                shutil.copy2(parent_md, thread_md)

    # ------------------------------------------------------------------
    # Generation lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _job_is_running(job: GenerationJob | None) -> bool:
        """Return True if the given generation job has an active (non-done) task."""
        return job is not None and job.task is not None and not job.task.done()

    def _start_generation(
        self,
        channel: discord.TextChannel | discord.Thread,
        channel_config: dict,
    ) -> None:
        """Create a new GenerationJob and schedule it on the event loop."""
        model_override = channel_config.get("model")
        job = GenerationJob()
        task = self.loop.create_task(
            self._generate_response(channel, job, model_override=model_override)
        )
        job.task = task
        self._active_generations[channel.id] = job

    def _interrupt_channel(
        self,
        message: discord.Message,
        existing_job: GenerationJob,
        channel_config: dict,
    ) -> None:
        """Cancel the running generation and start a fresh one.

        The active job dict entry is replaced *before* cancelling so the old
        task's ``finally`` block sees a different job and won't restart itself.
        """
        channel_id = message.channel.id

        # Swap in the new job before cancelling the old one.
        new_job = GenerationJob()
        self._active_generations[channel_id] = new_job
        existing_job.task.cancel()
        _LOG.info("Interrupted active generation for channel %s by %s",
                   channel_id, message.author.display_name)

        self._insert_synthetic_message(
            channel_id,
            "System",
            f"[{message.author.display_name} interrupted you. "
            f"Whatever you were doing may not be finished.]",
        )

        model_override = channel_config.get("model")
        new_task = self.loop.create_task(
            self._generate_response(message.channel, new_job, model_override=model_override)
        )
        new_job.task = new_task

    async def _maybe_update_presence(self) -> None:
        """Refresh the bot's Discord status with usage percentages (at most every 15 min)."""
        if time.monotonic() - self._presence_updated_at < _PRESENCE_INTERVAL:
            return
        try:
            week_pct_str, pace_str, resets_str = await self._fetch_usage_stats()
            status = f"{week_pct_str} wk | {pace_str}"
            if resets_str:
                status += f" | {resets_str}"
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=status,
                )
            )
            self._presence_updated_at = time.monotonic()
        except Exception as e:
            _LOG.error("Failed to update presence: %s", e)

    async def _fetch_usage_stats(self) -> tuple[str, str, str]:
        """Read cached usage data and return (week_pct_str, pace_str, resets_str).

        Reads from the usage_data.json file maintained by the TaskRunner's
        ``_check_usage`` loop.  Falls back to running get_usage.sh directly
        if the cached file is missing.

        pace = floor(elapsed_week_pct) - week_all_percent: positive means budget
        ahead of pace, negative means deficit. Updates ``_cached_usage`` on success.
        Returns ``("N/A", "N/A", "")`` on any failure.
        """
        global _cached_usage

        from .paths import WENDY_BASE
        usage_file = WENDY_BASE / "usage_data.json"

        data = None
        if usage_file.exists():
            try:
                data = json.loads(usage_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        if data is None:
            # Fallback: try running the script directly
            proc = await asyncio.create_subprocess_exec(
                "/app/scripts/get_usage.sh",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                _LOG.error("get_usage.sh failed: %s", stderr.decode("utf-8", errors="replace").strip())
                return "N/A", "N/A", ""
            try:
                data = json.loads(stdout.decode())
            except json.JSONDecodeError:
                return "N/A", "N/A", ""

        if USAGE_BUDGET_FACTOR < 1.0:
            for key in ("week_all_percent", "week_sonnet_percent", "session_percent"):
                if key in data:
                    data[key] = min(100, int(data[key] / USAGE_BUDGET_FACTOR))
        _cached_usage = data
        week_pct = data.get("week_all_percent")
        week_resets_str = data.get("week_all_resets", "")

        week_pct_str = f"{week_pct}%" if week_pct is not None else "N/A"
        pace_str = "N/A"
        resets_str = ""

        if week_pct is not None and week_resets_str:
            try:
                resets_at = datetime.datetime.fromisoformat(
                    week_resets_str.replace("Z", "+00:00")
                )
                now = datetime.datetime.now(datetime.UTC)
                # If resets_at is in the past, the cached percentage is from a
                # previous billing week.  Project the date forward and treat
                # usage as 0% for the new week.
                week_secs = 7 * 24 * 3600
                week_rolled = False
                while resets_at <= now:
                    resets_at += datetime.timedelta(seconds=week_secs)
                    week_rolled = True
                if week_rolled:
                    week_pct = 0
                    week_pct_str = "0%"
                    _cached_usage["week_all_percent"] = 0
                secs_remaining = (resets_at - now).total_seconds()
                elapsed_pct = int((1 - secs_remaining / week_secs) * 100)
                elapsed_pct = max(0, min(100, elapsed_pct))
                surplus = elapsed_pct - week_pct
                if surplus >= 0:
                    pace_str = f"+{surplus}% surplus"
                else:
                    pace_str = f"{abs(surplus)}% deficit"
                resets_str = resets_at.strftime("%b %-d")
            except Exception as e:
                _LOG.warning("Failed to compute surplus: %s", e)

        return week_pct_str, pace_str, resets_str

    async def _generate_response(
        self,
        channel: discord.TextChannel | discord.Thread,
        job: GenerationJob,
        model_override: str | None = None,
    ) -> None:
        """Run a single Claude CLI invocation for the given channel.

        On completion, if ``job.new_message_pending`` is set and unread messages
        exist, a follow-up generation is scheduled automatically.
        """
        channel_config = self.channel_configs.get(channel.id, {})

        await self._maybe_update_presence()

        try:
            from .config import resolve_model
            from .prompt import build_system_prompt

            system_prompt = build_system_prompt(channel.id, channel_config)
            resolved_model = resolve_model(channel_config.get("model") or "sonnet")
            effort_args = _get_current_effort(resolved_model)

            # Inject context introductions for newly relevant persons/topics.
            channel_name = _folder_for_config(channel_config)
            session_info = sessions.get_session(channel.id)
            if session_info:
                from .fragments import get_new_context_introductions
                from .fragments import get_recent_messages as _get_recent_msgs
                recent_msgs = _get_recent_msgs(channel.id)
                intros = get_new_context_introductions(
                    channel_name=channel_name,
                    session_id=session_info.session_id,
                    messages=recent_msgs,
                    channel_id=str(channel.id),
                )
                for intro in intros:
                    self._insert_synthetic_message(channel.id, "Context", intro)

            if job.is_enrichment:
                remaining = max(60.0, job.enrichment_end_timestamp - time.time())
                if job.enrichment_continuation:
                    enrichment_nudge = build_enrichment_continue_nudge(job.enrichment_end_time)
                else:
                    enrichment_nudge = build_enrichment_nudge(job.enrichment_end_time)
            else:
                enrichment_nudge = None
                remaining = None

            # Save the message watermark so we can restore it if the CLI
            # gets killed due to an overloaded error (the CLI's
            # check_messages call advances the watermark before we detect
            # the error, so the retry would see no messages).
            saved_last_seen = state_manager.get_last_seen(channel.id)

            await run_cli(
                channel_id=channel.id,
                channel_config=channel_config,
                system_prompt=system_prompt,
                model_override=model_override,
                effort_args=effort_args,
                nudge_override=enrichment_nudge,
                timeout_override=int(remaining) + 60 if remaining is not None else None,
                max_turns=100 if job.is_enrichment else None,
            )
            _LOG.info("CLI completed for channel %s", channel.id)

        except ClaudeCliError as e:
            if "timed out" in str(e).lower():
                job.timed_out = True
            if e.overloaded:
                # Restore the watermark so the retry sees the messages.
                if saved_last_seen is not None:
                    state_manager.update_last_seen(channel.id, saved_last_seen)
                if model_override != "opus":
                    _LOG.warning("Model overloaded for channel %s, waiting 10s then retrying with opus", channel.id)
                    await asyncio.sleep(10)
                    return await self._generate_response(channel, job, model_override="opus")
                elif not job.overload_retried:
                    # Opus also overloaded -- back off longer and retry once more.
                    job.overload_retried = True
                    _LOG.warning("Opus also overloaded for channel %s, waiting 60s then retrying", channel.id)
                    await asyncio.sleep(60)
                    return await self._generate_response(channel, job, model_override="opus")
                else:
                    _LOG.error("All models overloaded for channel %s, giving up", channel.id)
            self._handle_cli_error(channel, e)

        except Exception:
            _LOG.exception("Generation failed")

        finally:
            self._finalize_generation(channel, job)

    def _handle_cli_error(
        self,
        channel: discord.TextChannel | discord.Thread,
        error: ClaudeCliError,
    ) -> None:
        """Log or report a CLI error; notify the channel on OAuth expiry."""
        error_str = str(error).lower()
        if "oauth" in error_str and "expired" in error_str:
            asyncio.ensure_future(self._send_oauth_notice(channel))
        else:
            _LOG.error("Claude CLI error: %s", error)

    async def _send_oauth_notice(self, channel: discord.TextChannel | discord.Thread) -> None:
        """Send an OAuth-expiration message to the channel, swallowing errors."""
        try:
            await channel.send(
                "my claude cli token expired - someone needs to run "
                "`docker exec -it wendy claude login` to fix me"
            )
        except Exception:
            _LOG.exception("Failed to send OAuth expiration notice")

    def _finalize_generation(
        self,
        channel: discord.TextChannel | discord.Thread,
        job: GenerationJob,
    ) -> None:
        """Clean up after a generation: restart if messages are pending, else remove the job."""
        if self._active_generations.get(channel.id) is not job:
            return

        if job.is_enrichment:
            remaining = job.enrichment_end_timestamp - time.time()
            if remaining > 30:
                if job.enrichment_continuation_count >= _MAX_ENRICHMENT_CONTINUATIONS:
                    _LOG.warning(
                        "Enrichment hit max continuations (%d) for channel %s, stopping",
                        _MAX_ENRICHMENT_CONTINUATIONS, channel.id,
                    )
                else:
                    # Time still left -- re-invoke with a continuation nudge.
                    _LOG.info("Enrichment continuing for channel %s (%.0fs remaining, continuation %d)",
                              channel.id, remaining, job.enrichment_continuation_count + 1)
                    new_job = GenerationJob()
                    new_job.is_enrichment = True
                    new_job.enrichment_end_time = job.enrichment_end_time
                    new_job.enrichment_end_timestamp = job.enrichment_end_timestamp
                    new_job.enrichment_continuation = True
                    new_job.enrichment_continuation_count = job.enrichment_continuation_count + 1
                    new_task = self.loop.create_task(self._generate_response(channel, new_job))
                    new_job.task = new_task
                    self._active_generations[channel.id] = new_job
                    return
            # Enrichment over -- inject show-off nudge and start a generation.
            _LOG.info("Enrichment ended for channel %s", channel.id)
            self._insert_synthetic_message(channel.id, "System", build_enrichment_end_nudge())
            new_job = GenerationJob()
            new_task = self.loop.create_task(self._generate_response(channel, new_job))
            new_job.task = new_task
            self._active_generations[channel.id] = new_job
            return

        # Auto-continue if the CLI timed out (up to _MAX_TIMEOUT_CONTINUATIONS).
        if job.timed_out and job.continuation_count < _MAX_TIMEOUT_CONTINUATIONS:
            _LOG.info(
                "CLI timed out for channel %s (continuation %d/%d), auto-continuing",
                channel.id, job.continuation_count + 1, _MAX_TIMEOUT_CONTINUATIONS,
            )
            self._insert_synthetic_message(
                channel.id, "System",
                "[Your CLI session was interrupted because it hit the time limit. "
                "Pick up where you left off -- check messages first.]",
            )
            channel_config = self.channel_configs.get(channel.id, {})
            new_job = GenerationJob()
            new_job.continuation_count = job.continuation_count + 1
            # Carry over pending flag so messages aren't lost.
            new_job.new_message_pending = job.new_message_pending
            new_task = self.loop.create_task(
                self._generate_response(channel, new_job, model_override=channel_config.get("model"))
            )
            new_job.task = new_task
            self._active_generations[channel.id] = new_job
            return

        if job.new_message_pending and self._has_pending_messages(channel.id):
            _LOG.info("New messages pending in channel %s, starting new generation", channel.id)
            new_job = GenerationJob()
            new_task = self.loop.create_task(self._generate_response(channel, new_job))
            new_job.task = new_task
            self._active_generations[channel.id] = new_job
        else:
            self._active_generations.pop(channel.id, None)

    def _has_pending_messages(self, channel_id: int) -> bool:
        """Return True if the channel has user messages newer than last_seen."""
        return state_manager.has_pending_messages(channel_id, self.user.id)

    # ------------------------------------------------------------------
    # Notification polling
    # ------------------------------------------------------------------

    @tasks.loop(seconds=5)
    async def watch_notifications(self) -> None:
        """Poll for unseen notifications, insert synthetic messages, and wake channels."""
        if not self.whitelist_channels:
            return
        try:
            unseen = state_manager.get_unseen_notifications_for_wendy()
            if not unseen:
                return

            _LOG.info("Found %d unseen notifications", len(unseen))
            channels_to_wake: set[int] = set()
            notification_ids: list[int] = []

            for notif in unseen:
                notification_ids.append(notif.id)
                if notif.type == "task_completion":
                    self._handle_task_notification(notif, channels_to_wake)
                elif notif.type == "webhook":
                    self._handle_webhook_notification(notif)

            if notification_ids:
                state_manager.mark_notifications_seen_by_wendy(notification_ids)

            self._wake_channels(channels_to_wake)
        except Exception as e:
            _LOG.error("Error watching notifications: %s", e)

    @watch_notifications.before_loop
    async def before_watch_notifications(self) -> None:
        """Delay notification polling until the bot is connected."""
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def check_enrichment_schedule(self) -> None:
        """Trigger enrichment for eligible channels when the scheduled time arrives."""
        now = datetime.datetime.now(datetime.UTC)
        if now.hour != ENRICHMENT_HOUR_UTC or now.minute != ENRICHMENT_MINUTE_UTC:
            return
        today = now.date()
        for channel_id, channel_config in self._enrichment_channels():
            existing_job = self._active_generations.get(channel_id)
            if self._job_is_running(existing_job):
                continue
            if self._enrichment_last_run_date.get(channel_id) == today:
                continue
            channel = self.get_channel(channel_id)
            if channel:
                self._start_enrichment(channel, channel_config)

    @check_enrichment_schedule.before_loop
    async def before_check_enrichment_schedule(self) -> None:
        """Delay enrichment scheduling until the bot is connected."""
        await self.wait_until_ready()

    def _enrichment_channels(self):
        """Yield (channel_id, config) pairs for channels with enrichment_enabled."""
        for channel_id, config in self.channel_configs.items():
            if config.get("enrichment_enabled"):
                yield channel_id, config

    @tasks.loop(minutes=1)
    async def send_feature_digest(self) -> None:
        """Send daily feature request digest to the admin channel each morning."""
        now = datetime.datetime.now(datetime.UTC)
        if now.hour != FEATURE_DIGEST_HOUR_UTC or now.minute != 0:
            return
        today = now.date()
        if self._feature_digest_last_date == today:
            return
        self._feature_digest_last_date = today

        from .api_server import _load_feature_requests
        pending = [r for r in _load_feature_requests() if r.get("status") == "pending"]
        if not pending:
            return

        channel_id = FEATURE_DIGEST_CHANNEL
        if not channel_id:
            for cid, cfg in self.channel_configs.items():
                if cfg.get("mode") == "full":
                    channel_id = cid
                    break
        if not channel_id:
            return

        channel = self.get_channel(channel_id)
        if not channel:
            return

        lines = [f"**Feature Requests** ({len(pending)} pending):"]
        for r in pending:
            lines.append(f"- **#{r['id']}** ({r['user']}): {r['request']}")

        try:
            await channel.send("\n".join(lines))
        except Exception as e:
            _LOG.error("Failed to send feature digest: %s", e)

    @send_feature_digest.before_loop
    async def before_send_feature_digest(self) -> None:
        await self.wait_until_ready()

    def is_enrichment_active(self, channel_id: int) -> bool:
        """Return True if an enrichment session is currently running for channel_id."""
        job = self._active_generations.get(channel_id)
        return self._job_is_running(job) and job.is_enrichment

    def _start_enrichment(
        self,
        channel: discord.TextChannel | discord.Thread,
        channel_config: dict,
        *,
        manual: bool = False,
    ) -> None:
        """Slot an enrichment generation into _active_generations."""
        channel_id = channel.id
        today = datetime.datetime.now(datetime.UTC).date()

        if not manual and self._enrichment_last_run_date.get(channel_id) == today:
            return

        self._enrichment_last_run_date[channel_id] = today
        self._enrichment_notified.discard(channel_id)


        # Tell her she has 8 hours so she scopes ambitious projects
        fake_end_dt = (
            datetime.datetime.now(datetime.UTC).replace(microsecond=0)
            + datetime.timedelta(hours=8)
        )
        end_time_str = fake_end_dt.strftime("%H:%M")

        model_override = channel_config.get("model")
        job = GenerationJob()
        job.is_enrichment = True
        job.enrichment_end_time = end_time_str
        job.enrichment_end_timestamp = time.time() + ENRICHMENT_DURATION
        task = self.loop.create_task(
            self._generate_response(channel, job, model_override=model_override)
        )
        job.task = task
        self._active_generations[channel_id] = job
        _LOG.info("Enrichment started for channel %d (manual=%s, until=%s UTC)", channel_id, manual, end_time_str)

    def _resolve_notification_channel(self, notif_channel_id: int | None) -> int | None:
        """Return a valid channel ID for a notification, falling back to any full-mode channel."""
        if notif_channel_id:
            return notif_channel_id
        for cid, cfg in self.channel_configs.items():
            if cfg.get("mode") == "full":
                return cid
        return next(iter(self.whitelist_channels), None)

    def _handle_task_notification(self, notif, channels_to_wake: set[int]) -> None:
        """Insert a synthetic message for a task completion and mark the channel for waking."""
        channel_id = self._resolve_notification_channel(notif.channel_id)
        if not channel_id:
            return

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

    def _handle_webhook_notification(self, notif) -> None:
        """Insert a synthetic message for a webhook (does not wake the bot)."""
        channel_id = notif.channel_id
        if not channel_id or channel_id not in self.whitelist_channels:
            return

        author = f"Webhook: {notif.source.title()}"
        payload_content = ""
        if notif.payload:
            raw_data = notif.payload.get("raw", notif.payload)
            if isinstance(raw_data, dict):
                payload_content = "\n" + json.dumps(raw_data, indent=2)
            elif raw_data:
                payload_content = f"\n{raw_data}"

        self._insert_synthetic_message(channel_id, author, f"[{author}] {notif.title}{payload_content}")

    def _wake_channels(self, channel_ids: set[int]) -> None:
        """Start or flag a generation for each channel that needs waking."""
        for channel_id in channel_ids:
            channel = self.get_channel(channel_id)
            if not channel:
                continue
            existing_job = self._active_generations.get(channel_id)
            if self._job_is_running(existing_job):
                existing_job.new_message_pending = True
            else:
                self._start_generation(channel, self.channel_configs.get(channel_id, {}))

    # ------------------------------------------------------------------
    # Self-wake scheduling
    # ------------------------------------------------------------------

    def schedule_wake(self, channel_id: int, delay_seconds: int, message: str) -> str:
        """Schedule a delayed self-wake for a channel. Returns the wake time as a string."""
        # Cancel any existing wake for this channel
        existing = self._pending_wakes.pop(channel_id, None)
        if existing is not None:
            existing.cancel()

        wake_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=delay_seconds)
        wake_time_str = wake_at.strftime("%H:%M:%S UTC")

        handle = self.loop.call_later(
            delay_seconds,
            lambda: self.loop.create_task(self._fire_wake(channel_id, message)),
        )
        self._pending_wakes[channel_id] = handle
        _LOG.info("Wake scheduled for channel %s in %ds (%s): %s",
                  channel_id, delay_seconds, wake_time_str, message[:80])
        return wake_time_str

    async def _fire_wake(self, channel_id: int, message: str) -> None:
        """Fire a scheduled wake: inject synthetic message and trigger generation."""
        self._pending_wakes.pop(channel_id, None)
        self._insert_synthetic_message(
            channel_id,
            "Self-Wake",
            f"[Scheduled wake] {message}",
        )
        _LOG.info("Self-wake fired for channel %s: %s", channel_id, message[:80])
        self._wake_channels({channel_id})

    def _insert_synthetic_message(
        self,
        channel_id: int,
        author: str,
        content: str,
        guild_id: int | None = None,
    ) -> None:
        """Insert a fake message into SQLite so it appears in check_messages responses.

        IDs start at 9e18 to stay out of the way of real Discord snowflakes.
        """
        global _synthetic_counter
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
        """Write all guild emojis to a JSON file for the ``/api/emojis`` endpoint."""
        await self.wait_until_ready()
        try:
            all_emojis = [
                {
                    "name": emoji.name,
                    "id": str(emoji.id),
                    "animated": emoji.animated,
                    "usage": f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>",
                }
                for guild in self.guilds
                for emoji in guild.emojis
            ]

            from .paths import SHARED_DIR
            (SHARED_DIR / "emojis.json").write_text(json.dumps(all_emojis))
            _LOG.info("Cached %d emojis", len(all_emojis))
        except Exception as e:
            _LOG.error("Failed to cache emojis: %s", e)
