"""Standalone message logger cog - logs all messages from whitelisted guilds.

This cog is designed to be self-contained and portable between Discord bots.
It maintains its own database schema and requires no external dependencies
beyond discord.py.

Configuration via environment variables:
    MESSAGE_LOGGER_GUILDS: Comma-separated guild IDs to log (required)
    MESSAGE_LOGGER_DB_PATH: Path to SQLite database (default: /data/wendy/wendy.db)

Schema (message_history table):
    message_id INTEGER PRIMARY KEY
    channel_id INTEGER NOT NULL
    guild_id INTEGER
    timestamp TEXT NOT NULL (ISO 8601)
    author_id INTEGER NOT NULL
    author_nickname TEXT
    is_bot INTEGER DEFAULT 0
    is_webhook INTEGER DEFAULT 0
    content TEXT
    attachment_urls TEXT (JSON array)
    reply_to_id INTEGER
    reactions TEXT (JSON array)

Functions:
    insert_synthetic_message() - Insert a synthetic message (e.g., from webhooks)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import discord
from discord.ext import commands

from .paths import DB_PATH as DEFAULT_DB_PATH_FROM_PATHS

_LOG = logging.getLogger(__name__)

# Counter to guarantee unique synthetic message IDs even within the same clock tick
_synthetic_counter = 0

# Default database path - from paths.py (shared/wendy.db)
# Allow override via MESSAGE_LOGGER_DB_PATH for backwards compatibility
_env_db_path = os.getenv("MESSAGE_LOGGER_DB_PATH")
DEFAULT_DB_PATH = Path(_env_db_path) if _env_db_path else DEFAULT_DB_PATH_FROM_PATHS


class MessageLoggerCog(commands.Cog):
    """Logs all messages from whitelisted guilds to SQLite."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = Path(os.getenv("MESSAGE_LOGGER_DB_PATH", str(DEFAULT_DB_PATH)))
        self.allowed_guilds = self._parse_guild_ids()

        if not self.allowed_guilds:
            _LOG.warning("MESSAGE_LOGGER_GUILDS not set - message logging disabled")
        else:
            self._init_db()
            _LOG.info(
                "MessageLoggerCog initialized with %d guild(s): %s",
                len(self.allowed_guilds),
                ", ".join(str(g) for g in self.allowed_guilds)
            )

    def _parse_guild_ids(self) -> set[int]:
        """Parse MESSAGE_LOGGER_GUILDS env var into a set of guild IDs."""
        raw = os.getenv("MESSAGE_LOGGER_GUILDS", "")
        if not raw.strip():
            return set()

        guild_ids = set()
        for part in raw.split(","):
            part = part.strip()
            if part:
                try:
                    guild_ids.add(int(part))
                except ValueError:
                    _LOG.warning("Invalid guild ID in MESSAGE_LOGGER_GUILDS: %s", part)
        return guild_ids

    def _init_db(self) -> None:
        """Initialize the SQLite database schema.

        Handles migration from older schemas by adding missing columns.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            # Create table if not exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_history (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    guild_id INTEGER,
                    timestamp TEXT NOT NULL,
                    author_id INTEGER,
                    author_nickname TEXT,
                    is_bot INTEGER DEFAULT 0,
                    is_webhook INTEGER DEFAULT 0,
                    content TEXT,
                    attachment_urls TEXT,
                    reply_to_id INTEGER,
                    reactions TEXT
                )
            """)

            # Migration: Add columns that might be missing in older schemas
            # These are added with defaults that work for existing data
            migration_columns = [
                ("guild_id", "INTEGER"),
                ("author_id", "INTEGER"),
                ("is_bot", "INTEGER DEFAULT 0"),
                ("is_webhook", "INTEGER DEFAULT 0"),
                ("reply_to_id", "INTEGER"),
            ]

            for col_name, col_type in migration_columns:
                try:
                    conn.execute(f"ALTER TABLE message_history ADD COLUMN {col_name} {col_type}")
                    _LOG.info("Added column %s to message_history", col_name)
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # Create indexes (IF NOT EXISTS handles existing indexes)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_history_channel_time
                ON message_history(channel_id, timestamp)
            """)
            # Only create author index if author_id exists
            try:
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_message_history_author
                    ON message_history(author_id)
                """)
            except sqlite3.OperationalError:
                pass  # Index creation failed, column might not exist yet
            try:
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_message_history_guild
                    ON message_history(guild_id)
                """)
            except sqlite3.OperationalError:
                pass  # Index creation failed

            # DUPLICATE SCHEMA WARNING: This is a copy of state_manager.py schema.
            # Primary source of truth is bot/state_manager.py._init_schema()
            # Also duplicated in wendy-sites/backend/main.py (notifications only)
            # If you modify these tables, update all 3 locations!
            conn.executescript("""
                -- Channel sessions (replaces session_state.json)
                CREATE TABLE IF NOT EXISTS channel_sessions (
                    channel_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    message_count INTEGER DEFAULT 0,
                    total_input_tokens INTEGER DEFAULT 0,
                    total_output_tokens INTEGER DEFAULT 0,
                    total_cache_read_tokens INTEGER DEFAULT 0,
                    total_cache_create_tokens INTEGER DEFAULT 0
                );

                -- Last seen message IDs (replaces message_check_state.json)
                CREATE TABLE IF NOT EXISTS channel_last_seen (
                    channel_id INTEGER PRIMARY KEY,
                    last_message_id INTEGER NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- Unified notifications table (replaces task_completions and webhook_events)
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    channel_id INTEGER,
                    title TEXT NOT NULL,
                    payload TEXT,
                    seen_by_wendy INTEGER DEFAULT 0,
                    seen_by_proxy INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- Legacy: Task completions (kept for migration, will be removed)
                CREATE TABLE IF NOT EXISTS task_completions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    duration TEXT,
                    completed_at TEXT NOT NULL,
                    notified INTEGER DEFAULT 0,
                    seen_by_wendy INTEGER DEFAULT 0,
                    seen_by_proxy INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- Legacy: Webhook events (kept for migration, will be removed)
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    payload TEXT,
                    processed INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- Usage state (replaces usage_state.json)
                CREATE TABLE IF NOT EXISTS usage_state (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- Thread registry (maps thread IDs to parent channels and folder names)
                CREATE TABLE IF NOT EXISTS thread_registry (
                    thread_id INTEGER PRIMARY KEY,
                    parent_channel_id INTEGER NOT NULL,
                    folder_name TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- Indexes for notifications table
                CREATE INDEX IF NOT EXISTS idx_notifications_unseen_wendy
                    ON notifications(seen_by_wendy) WHERE seen_by_wendy = 0;
                CREATE INDEX IF NOT EXISTS idx_notifications_unseen_proxy
                    ON notifications(seen_by_proxy) WHERE seen_by_proxy = 0;

                -- Legacy indexes (kept for migration)
                CREATE INDEX IF NOT EXISTS idx_task_completions_unseen_wendy
                    ON task_completions(seen_by_wendy) WHERE seen_by_wendy = 0;
                CREATE INDEX IF NOT EXISTS idx_task_completions_unseen_proxy
                    ON task_completions(seen_by_proxy) WHERE seen_by_proxy = 0;
                CREATE INDEX IF NOT EXISTS idx_webhook_events_unprocessed
                    ON webhook_events(processed) WHERE processed = 0;
            """)

            conn.commit()

        _LOG.info("Message logger database initialized at %s", self.db_path)

    def _log_message(self, message: discord.Message) -> None:
        """Log a message to the database."""
        try:
            # Build attachment URLs list
            attachment_urls = [att.url for att in message.attachments] if message.attachments else []

            # Get reply target if this is a reply
            reply_to_id = None
            if message.reference and message.reference.message_id:
                reply_to_id = message.reference.message_id

            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO message_history (
                        message_id, channel_id, guild_id, timestamp,
                        author_id, author_nickname, is_bot, is_webhook,
                        content, attachment_urls, reply_to_id, reactions
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    message.id,
                    message.channel.id,
                    message.guild.id if message.guild else None,
                    message.created_at.isoformat(),
                    message.author.id,
                    message.author.display_name,
                    1 if message.author.bot else 0,
                    1 if message.webhook_id else 0,
                    message.content,
                    json.dumps(attachment_urls) if attachment_urls else None,
                    reply_to_id,
                    None,  # reactions populated on edit/reaction events
                ))
                conn.commit()
        except Exception as e:
            _LOG.exception("Failed to log message %s: %s", message.id, e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Log incoming messages from whitelisted guilds."""
        # Skip if no guilds configured
        if not self.allowed_guilds:
            return

        # Skip DMs
        if not message.guild:
            return

        # Check guild whitelist
        if message.guild.id not in self.allowed_guilds:
            return

        # Log the message
        self._log_message(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Update message content when edited."""
        if not self.allowed_guilds:
            return

        # Check if we have guild_id (may not be present for DMs)
        if not payload.guild_id or payload.guild_id not in self.allowed_guilds:
            return

        # Only update if we have new content
        data = payload.data
        if "content" not in data:
            return

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE message_history
                    SET content = ?
                    WHERE message_id = ?
                """, (data["content"], payload.message_id))
                conn.commit()
        except Exception as e:
            _LOG.exception("Failed to update edited message %s: %s", payload.message_id, e)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Handle message deletion - currently does nothing (keeps record)."""
        # We keep deleted messages in the archive for historical record.
        # If you want to mark them as deleted, add a 'deleted_at' column and update here.
        pass

    def insert_synthetic_message(
        self,
        channel_id: int,
        author_nickname: str,
        content: str,
        guild_id: int | None = None,
    ) -> int:
        """Insert a synthetic message into the database.

        Used for webhook events and other system-generated messages that should
        appear in the message history for Claude to see.

        Args:
            channel_id: Target channel ID.
            author_nickname: Display name for the message author (e.g., "Webhook: GitHub").
            content: Message content.
            guild_id: Optional guild ID.

        Returns:
            The generated message ID.
        """
        # Generate a synthetic message ID using nanosecond timestamp + counter
        # to avoid collisions when multiple messages arrive in the same clock tick.
        # Uses a large positive base (9 * 10^18) plus timestamp to ensure:
        # 1. IDs are always greater than Discord snowflake IDs (which are ~10^18)
        # 2. IDs are unique and monotonically increasing
        # 3. IDs show up in check_messages (which filters by message_id > since_id)
        global _synthetic_counter
        _synthetic_counter += 1
        message_id = 9_000_000_000_000_000_000 + int(time.time_ns() // 1000) + _synthetic_counter

        timestamp = datetime.now(UTC).isoformat()

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO message_history (
                        message_id, channel_id, guild_id, timestamp,
                        author_id, author_nickname, is_bot, is_webhook,
                        content, attachment_urls, reply_to_id, reactions
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    message_id,
                    channel_id,
                    guild_id,
                    timestamp,
                    0,  # author_id - 0 for synthetic
                    author_nickname,
                    0,  # is_bot
                    1,  # is_webhook - mark as webhook for identification
                    content,
                    None,  # attachment_urls
                    None,  # reply_to_id
                    None,  # reactions
                ))
                conn.commit()
            _LOG.info("Inserted synthetic message %d in channel %d", message_id, channel_id)
        except Exception as e:
            _LOG.exception("Failed to insert synthetic message: %s", e)

        return message_id


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension setup function."""
    await bot.add_cog(MessageLoggerCog(bot))
