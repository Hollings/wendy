"""Tests for the StateManager SQLite state module."""

import tempfile
from pathlib import Path

import pytest

from wendy.models import SessionInfo
from wendy.state import StateManager


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    # Cleanup
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def state_manager(temp_db):
    """Create a StateManager with a temporary database."""
    return StateManager(db_path=temp_db)


class TestSessionManagement:
    """Tests for session-related methods."""

    def test_get_session_nonexistent(self, state_manager):
        """get_session returns None for nonexistent channel."""
        result = state_manager.get_session(123456789)
        assert result is None

    def test_create_and_get_session(self, state_manager):
        """create_session stores session, get_session retrieves it."""
        channel_id = 123456789
        session_id = "test-session-uuid"
        folder = "coding"

        state_manager.create_session(channel_id, session_id, folder)
        session = state_manager.get_session(channel_id)

        assert session is not None
        assert isinstance(session, SessionInfo)
        assert session.channel_id == channel_id
        assert session.session_id == session_id
        assert session.folder == folder
        assert session.message_count == 0
        assert session.total_input_tokens == 0

    def test_update_session_stats(self, state_manager):
        """update_session_stats increments counters correctly."""
        channel_id = 123456789
        state_manager.create_session(channel_id, "session-1", "coding")

        state_manager.update_session_stats(
            channel_id,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=25,
            cache_create_tokens=10,
        )

        session = state_manager.get_session(channel_id)
        assert session.message_count == 1
        assert session.total_input_tokens == 100
        assert session.total_output_tokens == 50
        assert session.total_cache_read_tokens == 25
        assert session.total_cache_create_tokens == 10

        # Update again
        state_manager.update_session_stats(channel_id, input_tokens=50)
        session = state_manager.get_session(channel_id)
        assert session.message_count == 2
        assert session.total_input_tokens == 150

    def test_create_session_replaces_existing(self, state_manager):
        """create_session replaces existing session for same channel."""
        channel_id = 123456789
        state_manager.create_session(channel_id, "session-1", "chat")
        state_manager.update_session_stats(channel_id, input_tokens=100)

        # Create new session - should replace
        state_manager.create_session(channel_id, "session-2", "coding")

        session = state_manager.get_session(channel_id)
        assert session.session_id == "session-2"
        assert session.folder == "coding"
        assert session.message_count == 0
        assert session.total_input_tokens == 0

    def test_get_session_stats_dict(self, state_manager):
        """get_session_stats returns dict format for backwards compatibility."""
        channel_id = 123456789
        state_manager.create_session(channel_id, "session-1", "coding")

        stats = state_manager.get_session_stats(channel_id)
        assert isinstance(stats, dict)
        assert stats["session_id"] == "session-1"
        assert stats["folder"] == "coding"

    def test_get_session_stats_nonexistent(self, state_manager):
        """get_session_stats returns None for nonexistent channel."""
        stats = state_manager.get_session_stats(999999999)
        assert stats is None

    def test_update_session_stats_nonexistent_is_noop(self, state_manager):
        """update_session_stats on nonexistent session does not create one."""
        state_manager.update_session_stats(999999999, input_tokens=100)
        session = state_manager.get_session(999999999)
        assert session is None


class TestLastSeen:
    """Tests for last_seen message ID tracking."""

    def test_get_last_seen_nonexistent(self, state_manager):
        """get_last_seen returns None for channel without state."""
        result = state_manager.get_last_seen(123456789)
        assert result is None

    def test_update_and_get_last_seen(self, state_manager):
        """update_last_seen stores value, get_last_seen retrieves it."""
        channel_id = 123456789
        message_id = 987654321

        state_manager.update_last_seen(channel_id, message_id)
        result = state_manager.get_last_seen(channel_id)

        assert result == message_id

    def test_update_last_seen_replaces(self, state_manager):
        """update_last_seen replaces previous value."""
        channel_id = 123456789

        state_manager.update_last_seen(channel_id, 100)
        state_manager.update_last_seen(channel_id, 200)

        result = state_manager.get_last_seen(channel_id)
        assert result == 200


class TestNotifications:
    """Tests for the unified notifications API."""

    def test_add_and_get_notifications(self, state_manager):
        """add_notification stores, get_unseen retrieves."""
        notif_id = state_manager.add_notification(
            type="task_completion",
            source="orchestrator",
            title="Test Task Completed",
            channel_id=123456789,
            payload={"task_id": "abc123", "status": "completed", "duration": "0:05:00"},
        )

        assert notif_id > 0

        unseen = state_manager.get_unseen_notifications_for_wendy()
        assert len(unseen) == 1
        assert unseen[0].type == "task_completion"
        assert unseen[0].source == "orchestrator"
        assert unseen[0].title == "Test Task Completed"
        assert unseen[0].channel_id == 123456789
        assert unseen[0].payload == {"task_id": "abc123", "status": "completed", "duration": "0:05:00"}
        assert unseen[0].seen_by_wendy is False
        assert unseen[0].seen_by_proxy is False

    def test_add_notification_without_channel(self, state_manager):
        """add_notification works with None channel_id."""
        state_manager.add_notification(
            type="task_completion",
            source="orchestrator",
            title="Task without channel",
        )

        unseen = state_manager.get_unseen_notifications_for_wendy()
        assert len(unseen) == 1
        assert unseen[0].channel_id is None

    def test_add_webhook_notification(self, state_manager):
        """add_notification works for webhook type."""
        state_manager.add_notification(
            type="webhook",
            source="github",
            title="hollings pushed to main",
            channel_id=987654321,
            payload={"event_type": "push", "raw": {"ref": "refs/heads/main"}},
        )

        unseen = state_manager.get_unseen_notifications_for_wendy()
        assert len(unseen) == 1
        assert unseen[0].type == "webhook"
        assert unseen[0].source == "github"
        assert unseen[0].payload["event_type"] == "push"

    def test_mark_notifications_seen_by_wendy(self, state_manager):
        """mark_notifications_seen_by_wendy updates flag."""
        id1 = state_manager.add_notification("task_completion", "orchestrator", "Task 1")
        id2 = state_manager.add_notification("webhook", "github", "Push event")

        state_manager.mark_notifications_seen_by_wendy([id1])

        unseen = state_manager.get_unseen_notifications_for_wendy()
        assert len(unseen) == 1
        assert unseen[0].id == id2

    def test_mark_notifications_seen_by_proxy(self, state_manager):
        """mark_notifications_seen_by_proxy updates flag."""
        id1 = state_manager.add_notification("task_completion", "orchestrator", "Task 1")

        state_manager.mark_notifications_seen_by_proxy([id1])

        unseen = state_manager.get_unseen_notifications_for_proxy()
        assert len(unseen) == 0

    def test_get_unseen_for_proxy(self, state_manager):
        """get_unseen_notifications_for_proxy returns separate seen state."""
        id1 = state_manager.add_notification("task_completion", "orchestrator", "Task 1")

        # Mark seen by wendy but not proxy
        state_manager.mark_notifications_seen_by_wendy([id1])

        # Wendy's list should be empty
        assert len(state_manager.get_unseen_notifications_for_wendy()) == 0

        # Proxy's list should still have it
        unseen_proxy = state_manager.get_unseen_notifications_for_proxy()
        assert len(unseen_proxy) == 1
        assert unseen_proxy[0].id == id1

    def test_cleanup_old_notifications(self, state_manager):
        """cleanup_old_notifications removes old entries."""
        for i in range(10):
            state_manager.add_notification(
                type="task_completion",
                source="orchestrator",
                title=f"Task {i}",
            )

        state_manager.cleanup_old_notifications(keep_count=5)

        conn = state_manager._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        assert count == 5

    def test_mark_notifications_seen_empty_list_is_noop(self, state_manager):
        """mark_notifications_seen_by_wendy with empty list is a no-op."""
        state_manager.add_notification("task_completion", "orchestrator", "Task 1")
        state_manager.mark_notifications_seen_by_wendy([])
        unseen = state_manager.get_unseen_notifications_for_wendy()
        assert len(unseen) == 1  # Still unseen

    def test_notifications_ordered_by_id(self, state_manager):
        """get_unseen returns notifications in id order (oldest first)."""
        id1 = state_manager.add_notification("task_completion", "orchestrator", "First")
        id2 = state_manager.add_notification("task_completion", "orchestrator", "Second")
        id3 = state_manager.add_notification("task_completion", "orchestrator", "Third")

        unseen = state_manager.get_unseen_notifications_for_wendy()
        assert len(unseen) == 3
        assert unseen[0].id == id1
        assert unseen[1].id == id2
        assert unseen[2].id == id3


class TestUsageState:
    """Tests for usage threshold tracking."""

    def test_get_usage_threshold_default(self, state_manager):
        """get_usage_threshold returns 0 for unset key."""
        result = state_manager.get_usage_threshold("nonexistent")
        assert result == 0

    def test_set_and_get_usage_threshold(self, state_manager):
        """set_usage_threshold stores, get_usage_threshold retrieves."""
        state_manager.set_usage_threshold("last_notified_week_all", 50)

        result = state_manager.get_usage_threshold("last_notified_week_all")
        assert result == 50

    def test_set_usage_threshold_updates(self, state_manager):
        """set_usage_threshold updates existing value."""
        state_manager.set_usage_threshold("key1", 10)
        state_manager.set_usage_threshold("key1", 20)

        result = state_manager.get_usage_threshold("key1")
        assert result == 20

class TestThreadSafety:
    """Tests for thread-safe operations."""

    def test_concurrent_session_updates(self, state_manager):
        """Multiple threads can update sessions safely."""
        import threading

        channel_id = 123456789
        state_manager.create_session(channel_id, "session-1", "coding")

        errors = []

        def update_stats():
            try:
                for _ in range(100):
                    state_manager.update_session_stats(channel_id, input_tokens=1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=update_stats) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

        session = state_manager.get_session(channel_id)
        assert session.message_count == 500
        assert session.total_input_tokens == 500
