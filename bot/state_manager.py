"""Unified state management for Wendy bot using SQLite.

This module consolidates all state that was previously scattered across JSON files:
- session_state.json -> channel_sessions table
- message_check_state.json -> channel_last_seen table
- task_completions.json + webhook_events.json -> notifications table
- usage_state.json -> usage_state table

Thread-safe SQLite access is achieved through connection pooling and proper locking.

Usage:
    from bot.state_manager import state

    # Sessions
    state.get_session(channel_id) -> SessionInfo | None
    state.create_session(channel_id, session_id, folder)
    state.update_session_stats(channel_id, input_tokens, output_tokens, cache_read, cache_create)

    # Last seen
    state.get_last_seen(channel_id) -> int | None
    state.update_last_seen(channel_id, message_id)

    # Notifications (unified task completions + webhook events)
    state.add_notification(type, source, title, channel_id=None, payload=None) -> int
    state.get_unseen_notifications_for_wendy() -> list[Notification]
    state.get_unseen_notifications_for_proxy() -> list[Notification]
    state.mark_notifications_seen_by_wendy(notification_ids)
    state.mark_notifications_seen_by_proxy(notification_ids)
    state.cleanup_old_notifications(keep_count=100)

    # Usage
    state.get_usage_threshold(key) -> int
    state.set_usage_threshold(key, value)

    # Legacy (deprecated - use notifications instead)
    state.add_task_completion(...)
    state.add_webhook_event(...)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import DB_PATH as DEFAULT_DB_PATH_FROM_PATHS

_LOG = logging.getLogger(__name__)

# Default database path - from paths.py (shared/wendy.db)
# Allow override via WENDY_DB_PATH for backwards compatibility
_env_db_path = os.getenv("WENDY_DB_PATH")
DEFAULT_DB_PATH = Path(_env_db_path) if _env_db_path else DEFAULT_DB_PATH_FROM_PATHS


@dataclass
class SessionInfo:
    """Information about a Claude CLI session for a channel.

    Attributes:
        channel_id: Discord channel ID.
        session_id: Claude CLI session UUID.
        folder: Working directory folder name.
        created_at: Unix timestamp when session was created.
        last_used_at: Unix timestamp of last use, or None.
        message_count: Number of messages in session.
        total_input_tokens: Cumulative input token count.
        total_output_tokens: Cumulative output token count.
        total_cache_read_tokens: Cumulative cache read tokens.
        total_cache_create_tokens: Cumulative cache creation tokens.
    """

    channel_id: int
    session_id: str
    folder: str
    created_at: int
    last_used_at: int | None
    message_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_create_tokens: int


@dataclass
class Notification:
    """A unified notification for task completions, webhooks, and future types.

    Attributes:
        id: Auto-incremented primary key.
        type: Notification type ('task_completion', 'webhook', etc.).
        source: Origin of notification ('orchestrator', 'github', etc.).
        channel_id: Target Discord channel ID, or None for default.
        title: Human-readable summary/title.
        payload: JSON dict with type-specific data.
        seen_by_wendy: Whether Wendy has seen this notification.
        seen_by_proxy: Whether proxy has seen this notification.
        created_at: ISO timestamp of creation.
    """

    id: int
    type: str
    source: str
    channel_id: int | None
    title: str
    payload: dict | None
    seen_by_wendy: bool
    seen_by_proxy: bool
    created_at: str


# Legacy dataclasses kept for backwards compatibility during migration
@dataclass
class TaskCompletion:
    """A completed orchestrator task notification.

    DEPRECATED: Use Notification with type='task_completion' instead.

    Attributes:
        id: Auto-incremented primary key.
        task_id: Beads task ID.
        title: Human-readable task title.
        status: Completion status (completed/failed).
        duration: Human-readable duration string.
        completed_at: ISO timestamp of completion.
        notified: Whether Discord notification was sent.
        seen_by_wendy: Whether Wendy has seen this completion.
        seen_by_proxy: Whether proxy has seen this completion.
    """

    id: int
    task_id: str
    title: str
    status: str
    duration: str
    completed_at: str
    notified: bool
    seen_by_wendy: bool
    seen_by_proxy: bool


@dataclass
class WebhookEvent:
    """An incoming webhook event to be processed.

    DEPRECATED: Use Notification with type='webhook' instead.

    Attributes:
        id: Auto-incremented primary key.
        source: Webhook source name (e.g., "github").
        channel_id: Discord channel ID to notify.
        summary: Human-readable event summary.
        payload: Raw JSON payload.
        processed: Whether the event has been processed.
        created_at: ISO timestamp of when event was received.
    """

    id: int
    source: str
    channel_id: int
    summary: str
    payload: str | None
    processed: bool
    created_at: str


class StateManager:
    """Thread-safe SQLite state manager.

    Uses a connection per thread with proper locking.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize the state manager.

        Args:
            db_path: Path to SQLite database. Uses WENDY_DB_PATH env or default.
        """
        self.db_path = db_path or DEFAULT_DB_PATH
        self._local = threading.local()
        self._lock = threading.Lock()
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a connection for the current thread."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
            )
            self._local.conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent access
            self._local.conn.execute("PRAGMA journal_mode=WAL")

        # Initialize schema if not done
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    self._init_schema(self._local.conn)
                    self._initialized = True

        return self._local.conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """Initialize database schema with all state tables.

        DUPLICATE SCHEMA WARNING: This schema is defined in 3 places:
          1. HERE (primary source of truth)
          2. bot/message_logger.py (copy for startup ordering)
          3. wendy-sites/backend/main.py (notifications only, separate container)
        If you modify these tables, update all locations!
        """
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
        _LOG.info("State manager schema initialized at %s", self.db_path)

    # =========================================================================
    # Session Management
    # =========================================================================

    def get_session(self, channel_id: int) -> SessionInfo | None:
        """Get session info for a channel.

        Args:
            channel_id: Discord channel ID.

        Returns:
            SessionInfo if session exists, None otherwise.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM channel_sessions WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()

        if not row:
            return None

        return SessionInfo(
            channel_id=row["channel_id"],
            session_id=row["session_id"],
            folder=row["folder"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            message_count=row["message_count"],
            total_input_tokens=row["total_input_tokens"],
            total_output_tokens=row["total_output_tokens"],
            total_cache_read_tokens=row["total_cache_read_tokens"],
            total_cache_create_tokens=row["total_cache_create_tokens"],
        )

    def create_session(
        self,
        channel_id: int,
        session_id: str,
        folder: str,
    ) -> None:
        """Create or replace a session for a channel.

        Args:
            channel_id: Discord channel ID.
            session_id: Claude CLI session UUID.
            folder: Working directory folder name.
        """
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO channel_sessions
                (channel_id, session_id, folder, created_at, message_count,
                 total_input_tokens, total_output_tokens,
                 total_cache_read_tokens, total_cache_create_tokens)
            VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0)
            """,
            (channel_id, session_id, folder, int(time.time()))
        )
        conn.commit()
        _LOG.info(
            "Created session %s for channel %d (folder=%s)",
            session_id[:8], channel_id, folder
        )

    def update_session_stats(
        self,
        channel_id: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_create_tokens: int = 0,
    ) -> None:
        """Update session statistics after a CLI run.

        Args:
            channel_id: Discord channel ID.
            input_tokens: Input tokens used in this run.
            output_tokens: Output tokens used in this run.
            cache_read_tokens: Cache read tokens in this run.
            cache_create_tokens: Cache creation tokens in this run.
        """
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE channel_sessions
            SET message_count = message_count + 1,
                total_input_tokens = total_input_tokens + ?,
                total_output_tokens = total_output_tokens + ?,
                total_cache_read_tokens = total_cache_read_tokens + ?,
                total_cache_create_tokens = total_cache_create_tokens + ?,
                last_used_at = ?
            WHERE channel_id = ?
            """,
            (
                input_tokens, output_tokens,
                cache_read_tokens, cache_create_tokens,
                int(time.time()), channel_id
            )
        )
        conn.commit()

    def get_session_stats(self, channel_id: int) -> dict[str, Any] | None:
        """Get session stats as a dictionary (for backwards compatibility).

        Args:
            channel_id: Discord channel ID.

        Returns:
            Dict with session info, or None if no session exists.
        """
        session = self.get_session(channel_id)
        if not session:
            return None

        return {
            "session_id": session.session_id,
            "folder": session.folder,
            "created_at": session.created_at,
            "last_used_at": session.last_used_at,
            "message_count": session.message_count,
            "total_input_tokens": session.total_input_tokens,
            "total_output_tokens": session.total_output_tokens,
            "total_cache_read_tokens": session.total_cache_read_tokens,
            "total_cache_create_tokens": session.total_cache_create_tokens,
        }

    # =========================================================================
    # Last Seen Message ID
    # =========================================================================

    def get_last_seen(self, channel_id: int) -> int | None:
        """Get the last seen message ID for a channel.

        Args:
            channel_id: Discord channel ID.

        Returns:
            Last seen message ID, or None if not set.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT last_message_id FROM channel_last_seen WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()
        return row["last_message_id"] if row else None

    def update_last_seen(self, channel_id: int, message_id: int) -> None:
        """Update the last seen message ID for a channel.

        Args:
            channel_id: Discord channel ID.
            message_id: Newest seen message ID.
        """
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO channel_last_seen (channel_id, last_message_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (channel_id, message_id)
        )
        conn.commit()

    # =========================================================================
    # Task Completions (DEPRECATED - use Notifications API instead)
    # =========================================================================

    def add_task_completion(
        self,
        task_id: str,
        title: str,
        status: str = "completed",
        duration: str = "",
    ) -> int:
        """Record a task completion.

        DEPRECATED: Use add_notification(type='task_completion', ...) instead.

        Args:
            task_id: Beads task ID.
            title: Human-readable task title.
            status: Completion status (completed/failed).
            duration: Human-readable duration string.

        Returns:
            The auto-generated row ID.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """
            INSERT INTO task_completions
                (task_id, title, status, duration, completed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                duration = excluded.duration,
                completed_at = excluded.completed_at
            """,
            (task_id, title, status, duration, datetime.now(UTC).isoformat())
        )
        conn.commit()
        _LOG.info("Recorded task completion: %s (%s)", task_id, status)
        return cursor.lastrowid

    def get_unseen_completions_for_wendy(self) -> list[TaskCompletion]:
        """Get task completions not yet seen by Wendy.

        DEPRECATED: Use get_unseen_notifications_for_wendy() instead.

        Returns:
            List of unseen TaskCompletion objects.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM task_completions WHERE seen_by_wendy = 0"
        ).fetchall()

        return [
            TaskCompletion(
                id=row["id"],
                task_id=row["task_id"],
                title=row["title"],
                status=row["status"],
                duration=row["duration"] or "",
                completed_at=row["completed_at"],
                notified=bool(row["notified"]),
                seen_by_wendy=bool(row["seen_by_wendy"]),
                seen_by_proxy=bool(row["seen_by_proxy"]),
            )
            for row in rows
        ]

    def mark_completions_seen_by_wendy(self, task_ids: list[str]) -> None:
        """Mark task completions as seen by Wendy.

        DEPRECATED: Use mark_notifications_seen_by_wendy() instead.

        Args:
            task_ids: List of task IDs to mark as seen.
        """
        if not task_ids:
            return

        conn = self._get_conn()
        placeholders = ",".join("?" * len(task_ids))
        conn.execute(
            f"UPDATE task_completions SET seen_by_wendy = 1 WHERE task_id IN ({placeholders})",
            task_ids
        )
        conn.commit()

    def get_unseen_completions_for_proxy(self) -> list[TaskCompletion]:
        """Get task completions not yet seen by proxy.

        DEPRECATED: Use get_unseen_notifications_for_proxy() instead.

        Returns:
            List of unseen TaskCompletion objects.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM task_completions WHERE seen_by_proxy = 0"
        ).fetchall()

        return [
            TaskCompletion(
                id=row["id"],
                task_id=row["task_id"],
                title=row["title"],
                status=row["status"],
                duration=row["duration"] or "",
                completed_at=row["completed_at"],
                notified=bool(row["notified"]),
                seen_by_wendy=bool(row["seen_by_wendy"]),
                seen_by_proxy=bool(row["seen_by_proxy"]),
            )
            for row in rows
        ]

    def mark_completions_seen_by_proxy(self, task_ids: list[str]) -> None:
        """Mark task completions as seen by proxy.

        DEPRECATED: Use mark_notifications_seen_by_proxy() instead.

        Args:
            task_ids: List of task IDs to mark as seen.
        """
        if not task_ids:
            return

        conn = self._get_conn()
        placeholders = ",".join("?" * len(task_ids))
        conn.execute(
            f"UPDATE task_completions SET seen_by_proxy = 1 WHERE task_id IN ({placeholders})",
            task_ids
        )
        conn.commit()

    def mark_completion_notified(self, task_id: str) -> None:
        """Mark a task completion as having been notified to Discord.

        DEPRECATED: No longer needed with unified notifications.

        Args:
            task_id: The task ID to mark as notified.
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE task_completions SET notified = 1 WHERE task_id = ?",
            (task_id,)
        )
        conn.commit()

    def cleanup_old_completions(self, keep_count: int = 50) -> None:
        """Remove old task completions, keeping only the most recent.

        DEPRECATED: Use cleanup_old_notifications() instead.

        Args:
            keep_count: Number of recent completions to keep.
        """
        conn = self._get_conn()
        conn.execute(
            """
            DELETE FROM task_completions
            WHERE id NOT IN (
                SELECT id FROM task_completions
                ORDER BY completed_at DESC
                LIMIT ?
            )
            """,
            (keep_count,)
        )
        conn.commit()

    # =========================================================================
    # Webhook Events (DEPRECATED - use Notifications API instead)
    # =========================================================================

    def add_webhook_event(
        self,
        source: str,
        channel_id: int,
        summary: str,
        payload: dict | None = None,
    ) -> int:
        """Record a webhook event for processing.

        DEPRECATED: Use add_notification(type='webhook', ...) instead.

        Args:
            source: Webhook source name (e.g., "github").
            channel_id: Discord channel ID to notify.
            summary: Human-readable event summary.
            payload: Optional raw JSON payload.

        Returns:
            The auto-generated row ID.
        """
        conn = self._get_conn()
        payload_str = json.dumps(payload) if payload else None
        cursor = conn.execute(
            """
            INSERT INTO webhook_events (source, channel_id, summary, payload)
            VALUES (?, ?, ?, ?)
            """,
            (source, channel_id, summary, payload_str)
        )
        conn.commit()
        _LOG.info("Recorded webhook event from %s for channel %d", source, channel_id)
        return cursor.lastrowid

    def get_unprocessed_webhook_events(self) -> list[WebhookEvent]:
        """Get webhook events that haven't been processed yet.

        DEPRECATED: Use get_unseen_notifications_for_wendy() instead.

        Returns:
            List of unprocessed WebhookEvent objects.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM webhook_events WHERE processed = 0 ORDER BY id ASC"
        ).fetchall()

        return [
            WebhookEvent(
                id=row["id"],
                source=row["source"],
                channel_id=row["channel_id"],
                summary=row["summary"],
                payload=row["payload"],
                processed=bool(row["processed"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def mark_webhook_events_processed(self, event_ids: list[int]) -> None:
        """Mark webhook events as processed.

        DEPRECATED: Use mark_notifications_seen_by_wendy() instead.

        Args:
            event_ids: List of event IDs to mark as processed.
        """
        if not event_ids:
            return

        conn = self._get_conn()
        placeholders = ",".join("?" * len(event_ids))
        conn.execute(
            f"UPDATE webhook_events SET processed = 1 WHERE id IN ({placeholders})",
            event_ids
        )
        conn.commit()

    def cleanup_old_webhook_events(self, keep_count: int = 100) -> None:
        """Remove old webhook events, keeping only the most recent.

        DEPRECATED: Use cleanup_old_notifications() instead.

        Args:
            keep_count: Number of recent events to keep.
        """
        conn = self._get_conn()
        conn.execute(
            """
            DELETE FROM webhook_events
            WHERE id NOT IN (
                SELECT id FROM webhook_events
                ORDER BY created_at DESC
                LIMIT ?
            )
            """,
            (keep_count,)
        )
        conn.commit()

    # =========================================================================
    # Unified Notifications (replaces Task Completions + Webhook Events)
    # =========================================================================

    def add_notification(
        self,
        type: str,
        source: str,
        title: str,
        channel_id: int | None = None,
        payload: dict | None = None,
    ) -> int:
        """Add a notification to the unified notifications table.

        Args:
            type: Notification type ('task_completion', 'webhook', etc.).
            source: Origin of notification ('orchestrator', 'github', etc.).
            title: Human-readable summary/title.
            channel_id: Target Discord channel ID, or None for default.
            payload: Optional dict with type-specific data.

        Returns:
            The auto-generated row ID.
        """
        conn = self._get_conn()
        payload_str = json.dumps(payload) if payload else None
        cursor = conn.execute(
            """
            INSERT INTO notifications (type, source, channel_id, title, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (type, source, channel_id, title, payload_str)
        )
        conn.commit()
        _LOG.info("Added notification: type=%s source=%s title=%s", type, source, title)
        return cursor.lastrowid

    def get_unseen_notifications_for_wendy(self) -> list[Notification]:
        """Get notifications not yet seen by Wendy.

        Returns:
            List of unseen Notification objects, ordered by id ascending.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM notifications WHERE seen_by_wendy = 0 ORDER BY id ASC"
        ).fetchall()

        return [
            Notification(
                id=row["id"],
                type=row["type"],
                source=row["source"],
                channel_id=row["channel_id"],
                title=row["title"],
                payload=json.loads(row["payload"]) if row["payload"] else None,
                seen_by_wendy=bool(row["seen_by_wendy"]),
                seen_by_proxy=bool(row["seen_by_proxy"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def get_unseen_notifications_for_proxy(self) -> list[Notification]:
        """Get notifications not yet seen by proxy.

        Returns:
            List of unseen Notification objects, ordered by id ascending.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM notifications WHERE seen_by_proxy = 0 ORDER BY id ASC"
        ).fetchall()

        return [
            Notification(
                id=row["id"],
                type=row["type"],
                source=row["source"],
                channel_id=row["channel_id"],
                title=row["title"],
                payload=json.loads(row["payload"]) if row["payload"] else None,
                seen_by_wendy=bool(row["seen_by_wendy"]),
                seen_by_proxy=bool(row["seen_by_proxy"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def mark_notifications_seen_by_wendy(self, notification_ids: list[int]) -> None:
        """Mark notifications as seen by Wendy.

        Args:
            notification_ids: List of notification IDs to mark as seen.
        """
        if not notification_ids:
            return

        conn = self._get_conn()
        placeholders = ",".join("?" * len(notification_ids))
        conn.execute(
            f"UPDATE notifications SET seen_by_wendy = 1 WHERE id IN ({placeholders})",
            notification_ids
        )
        conn.commit()

    def mark_notifications_seen_by_proxy(self, notification_ids: list[int]) -> None:
        """Mark notifications as seen by proxy.

        Args:
            notification_ids: List of notification IDs to mark as seen.
        """
        if not notification_ids:
            return

        conn = self._get_conn()
        placeholders = ",".join("?" * len(notification_ids))
        conn.execute(
            f"UPDATE notifications SET seen_by_proxy = 1 WHERE id IN ({placeholders})",
            notification_ids
        )
        conn.commit()

    def cleanup_old_notifications(self, keep_count: int = 100) -> None:
        """Remove old notifications, keeping only the most recent.

        Args:
            keep_count: Number of recent notifications to keep.
        """
        conn = self._get_conn()
        conn.execute(
            """
            DELETE FROM notifications
            WHERE id NOT IN (
                SELECT id FROM notifications
                ORDER BY created_at DESC
                LIMIT ?
            )
            """,
            (keep_count,)
        )
        conn.commit()

    # =========================================================================
    # Usage State
    # =========================================================================

    def get_usage_threshold(self, key: str) -> int:
        """Get a usage threshold value.

        Args:
            key: The threshold key (e.g., "last_notified_week_all").

        Returns:
            The threshold value, or 0 if not set.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM usage_state WHERE key = ?",
            (key,)
        ).fetchone()
        return row["value"] if row else 0

    def set_usage_threshold(self, key: str, value: int) -> None:
        """Set a usage threshold value.

        Args:
            key: The threshold key.
            value: The threshold value.
        """
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO usage_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (key, value)
        )
        conn.commit()

    def get_all_usage_state(self) -> dict[str, int]:
        """Get all usage state as a dictionary.

        Returns:
            Dict mapping keys to values.
        """
        conn = self._get_conn()
        rows = conn.execute("SELECT key, value FROM usage_state").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # =========================================================================
    # Migration Helpers
    # =========================================================================

    def migrate_from_session_json(self, json_path: Path) -> int:
        """Migrate session state from JSON file to SQLite.

        Args:
            json_path: Path to session_state.json file.

        Returns:
            Number of sessions migrated.
        """
        if not json_path.exists():
            return 0

        try:
            data = json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _LOG.warning("Failed to read session JSON: %s", e)
            return 0

        count = 0
        conn = self._get_conn()
        for channel_id_str, session_info in data.items():
            try:
                channel_id = int(channel_id_str)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_sessions
                        (channel_id, session_id, folder, created_at, last_used_at,
                         message_count, total_input_tokens, total_output_tokens,
                         total_cache_read_tokens, total_cache_create_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel_id,
                        session_info.get("session_id", ""),
                        session_info.get("folder", "wendys_folder"),
                        session_info.get("created_at", int(time.time())),
                        session_info.get("last_used_at"),
                        session_info.get("message_count", 0),
                        session_info.get("total_input_tokens", 0),
                        session_info.get("total_output_tokens", 0),
                        session_info.get("total_cache_read_tokens", 0),
                        session_info.get("total_cache_create_tokens", 0),
                    )
                )
                count += 1
            except (ValueError, KeyError) as e:
                _LOG.warning("Failed to migrate session %s: %s", channel_id_str, e)

        conn.commit()
        _LOG.info("Migrated %d sessions from JSON", count)

        # Rename old file to prevent re-migration
        backup_path = json_path.with_suffix(".json.migrated")
        json_path.rename(backup_path)
        _LOG.info("Renamed %s to %s", json_path, backup_path)

        return count

    def migrate_from_usage_json(self, json_path: Path) -> bool:
        """Migrate usage state from JSON file to SQLite.

        Args:
            json_path: Path to usage_state.json file.

        Returns:
            True if migration succeeded.
        """
        if not json_path.exists():
            return False

        try:
            data = json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _LOG.warning("Failed to read usage JSON: %s", e)
            return False

        conn = self._get_conn()
        for key, value in data.items():
            if isinstance(value, int):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO usage_state (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    """,
                    (key, value)
                )

        conn.commit()
        _LOG.info("Migrated usage state from JSON")

        # Rename old file
        backup_path = json_path.with_suffix(".json.migrated")
        json_path.rename(backup_path)

        return True


# Global singleton instance
state = StateManager()
