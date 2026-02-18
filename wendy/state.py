"""Unified SQLite state manager.

ONE schema definition. No duplicates anywhere.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from .models import Notification, SessionInfo
from .paths import DB_PATH as DEFAULT_DB_PATH

_LOG = logging.getLogger(__name__)

_env_db_path = os.getenv("WENDY_DB_PATH")
_DEFAULT_DB_PATH = Path(_env_db_path) if _env_db_path else DEFAULT_DB_PATH


class StateManager:
    """Thread-safe SQLite state manager with a single schema definition."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _DEFAULT_DB_PATH
        self._local = threading.local()
        self._lock = threading.Lock()
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")

        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    self._init_schema(self._local.conn)
                    self._initialized = True

        return self._local.conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """Initialize database schema. THIS IS THE ONLY SCHEMA DEFINITION."""
        conn.executescript("""
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

            CREATE TABLE IF NOT EXISTS channel_last_seen (
                channel_id INTEGER PRIMARY KEY,
                last_message_id INTEGER NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS message_history (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER,
                author_id INTEGER,
                author_nickname TEXT,
                is_bot INTEGER DEFAULT 0,
                is_webhook INTEGER DEFAULT 0,
                content TEXT,
                timestamp INTEGER,
                attachment_urls TEXT,
                reply_to_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_message_history_channel
                ON message_history(channel_id, message_id);

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

            CREATE INDEX IF NOT EXISTS idx_notifications_unseen_wendy
                ON notifications(seen_by_wendy) WHERE seen_by_wendy = 0;
            CREATE INDEX IF NOT EXISTS idx_notifications_unseen_proxy
                ON notifications(seen_by_proxy) WHERE seen_by_proxy = 0;

            CREATE TABLE IF NOT EXISTS thread_registry (
                thread_id INTEGER PRIMARY KEY,
                parent_channel_id INTEGER NOT NULL,
                folder_name TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS usage_state (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bash_tool_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                command TEXT NOT NULL,
                description TEXT,
                cwd TEXT,
                exit_code INTEGER,
                output TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_bash_tool_log_session
                ON bash_tool_log(session_id);
            CREATE INDEX IF NOT EXISTS idx_bash_tool_log_created
                ON bash_tool_log(created_at);

            CREATE TABLE IF NOT EXISTS session_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                folder TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                message_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_session_history_channel
                ON session_history(channel_id, started_at);
        """)
        conn.commit()
        _LOG.info("Schema initialized at %s", self.db_path)

    # =========================================================================
    # Session Management
    # =========================================================================

    def get_session(self, channel_id: int) -> SessionInfo | None:
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

    def create_session(self, channel_id: int, session_id: str, folder: str) -> None:
        conn = self._get_conn()
        now = int(time.time())

        existing = conn.execute(
            "SELECT * FROM channel_sessions WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                INSERT OR IGNORE INTO session_history
                    (channel_id, session_id, folder, started_at, ended_at,
                     message_count, total_input_tokens, total_output_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (existing["channel_id"], existing["session_id"], existing["folder"],
                 existing["created_at"], now,
                 existing["message_count"], existing["total_input_tokens"],
                 existing["total_output_tokens"])
            )

        conn.execute(
            """
            INSERT OR REPLACE INTO channel_sessions
                (channel_id, session_id, folder, created_at, message_count,
                 total_input_tokens, total_output_tokens,
                 total_cache_read_tokens, total_cache_create_tokens)
            VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0)
            """,
            (channel_id, session_id, folder, now)
        )
        conn.commit()
        _LOG.info("Created session %s for channel %d (folder=%s)", session_id[:8], channel_id, folder)

    def update_session_stats(
        self,
        channel_id: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_create_tokens: int = 0,
    ) -> None:
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
            (input_tokens, output_tokens, cache_read_tokens, cache_create_tokens,
             int(time.time()), channel_id)
        )
        conn.commit()

    def get_session_stats(self, channel_id: int) -> dict | None:
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
        conn = self._get_conn()
        row = conn.execute(
            "SELECT last_message_id FROM channel_last_seen WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()
        return row["last_message_id"] if row else None

    def update_last_seen(self, channel_id: int, message_id: int) -> None:
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
    # Message History
    # =========================================================================

    def insert_message(
        self,
        message_id: int,
        channel_id: int,
        guild_id: int | None,
        author_id: int | None,
        author_nickname: str | None,
        is_bot: bool,
        content: str | None,
        timestamp: int | None,
        attachment_urls: str | None = None,
        reply_to_id: int | None = None,
        is_webhook: bool = False,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR IGNORE INTO message_history
                (message_id, channel_id, guild_id, author_id, author_nickname,
                 is_bot, is_webhook, content, timestamp, attachment_urls, reply_to_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, channel_id, guild_id, author_id, author_nickname,
             int(is_bot), int(is_webhook), content, timestamp, attachment_urls, reply_to_id)
        )
        conn.commit()

    def update_message_content(self, message_id: int, content: str) -> None:
        """Update message content (for edits)."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE message_history SET content = ? WHERE message_id = ?",
            (content, message_id)
        )
        conn.commit()

    def get_recent_messages(self, channel_id: int, limit: int = 50) -> list[dict]:
        """Get recent messages for a channel (for fragment keyword matching)."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT message_id, author_id, author_nickname as author, content, timestamp
            FROM message_history
            WHERE channel_id = ?
            ORDER BY message_id DESC
            LIMIT ?
            """,
            (channel_id, limit)
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def delete_messages(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        conn = self._get_conn()
        placeholders = ",".join("?" * len(message_ids))
        conn.execute(
            f"DELETE FROM message_history WHERE message_id IN ({placeholders})",
            message_ids
        )
        conn.commit()

    # =========================================================================
    # Notifications
    # =========================================================================

    def add_notification(
        self,
        type: str,
        source: str,
        title: str,
        channel_id: int | None = None,
        payload: dict | None = None,
    ) -> int:
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
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM notifications WHERE seen_by_wendy = 0 ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_notification(row) for row in rows]

    def get_unseen_notifications_for_proxy(self) -> list[Notification]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM notifications WHERE seen_by_proxy = 0 ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_notification(row) for row in rows]

    def _row_to_notification(self, row: sqlite3.Row) -> Notification:
        return Notification(
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

    def mark_notifications_seen_by_wendy(self, notification_ids: list[int]) -> None:
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
    # Thread Registry
    # =========================================================================

    def register_thread(self, thread_id: int, parent_channel_id: int, folder_name: str) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR IGNORE INTO thread_registry (thread_id, parent_channel_id, folder_name)
            VALUES (?, ?, ?)
            """,
            (thread_id, parent_channel_id, folder_name)
        )
        conn.commit()

    def get_thread_folder(self, thread_id: int) -> str | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT folder_name FROM thread_registry WHERE thread_id = ?",
            (thread_id,)
        ).fetchone()
        return row["folder_name"] if row else None

    def get_thread_parent(self, thread_id: int) -> int | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT parent_channel_id FROM thread_registry WHERE thread_id = ?",
            (thread_id,)
        ).fetchone()
        return row["parent_channel_id"] if row else None

    # =========================================================================
    # Usage State
    # =========================================================================

    def get_usage_threshold(self, key: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM usage_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else 0

    def set_usage_threshold(self, key: str, value: int) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO usage_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (key, value)
        )
        conn.commit()

    # =========================================================================
    # Session History
    # =========================================================================

    def get_session_history(self, channel_id: int, limit: int = 10) -> list[dict]:
        """Return recent archived sessions for a channel, newest first."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM session_history
            WHERE channel_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (channel_id, limit)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_session_by_id(self, session_id_prefix: str) -> dict | None:
        """Look up a session by ID or prefix. Checks history then active sessions."""
        conn = self._get_conn()

        row = conn.execute(
            "SELECT * FROM session_history WHERE session_id = ?",
            (session_id_prefix,)
        ).fetchone()
        if row:
            return dict(row)

        row = conn.execute(
            "SELECT * FROM session_history WHERE session_id LIKE ?",
            (session_id_prefix + "%",)
        ).fetchone()
        if row:
            return dict(row)

        row = conn.execute(
            """
            SELECT channel_id, session_id, folder, created_at AS started_at
            FROM channel_sessions
            WHERE session_id LIKE ?
            """,
            (session_id_prefix + "%",)
        ).fetchone()
        if row:
            return dict(row)

        return None


# Global singleton
state = StateManager()
