"""Tests for wendy.state."""
from __future__ import annotations

from wendy.state import StateManager


def _make_sm(tmp_path) -> StateManager:
    sm = StateManager(db_path=tmp_path / "test.db")
    sm._get_conn()  # trigger schema init
    return sm


# =========================================================================
# Session management
# =========================================================================


def test_create_and_get_session(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "sess-abc", "general")

    info = sm.get_session(123)
    assert info is not None
    assert info.session_id == "sess-abc"
    assert info.folder == "general"
    assert info.message_count == 0


def test_get_session_returns_none_for_missing(tmp_path):
    sm = _make_sm(tmp_path)
    assert sm.get_session(999) is None


def test_create_session_replaces_existing(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "sess-1", "general")
    sm.create_session(123, "sess-2", "general")

    info = sm.get_session(123)
    assert info.session_id == "sess-2"
    assert info.message_count == 0


def test_update_session_stats(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "sess-abc", "general")

    sm.update_session_stats(123, input_tokens=100, output_tokens=50)
    info = sm.get_session(123)
    assert info.message_count == 1
    assert info.total_input_tokens == 100
    assert info.total_output_tokens == 50

    sm.update_session_stats(123, input_tokens=200, output_tokens=75, cache_read_tokens=10)
    info = sm.get_session(123)
    assert info.message_count == 2
    assert info.total_input_tokens == 300
    assert info.total_output_tokens == 125
    assert info.total_cache_read_tokens == 10


def test_get_session_stats(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "sess-abc", "general")
    sm.update_session_stats(123, input_tokens=50, output_tokens=25)

    stats = sm.get_session_stats(123)
    assert stats is not None
    assert stats["session_id"] == "sess-abc"
    assert stats["message_count"] == 1
    assert sm.get_session_stats(999) is None


# =========================================================================
# Last seen
# =========================================================================


def test_last_seen(tmp_path):
    sm = _make_sm(tmp_path)
    assert sm.get_last_seen(123) is None

    sm.update_last_seen(123, 456)
    assert sm.get_last_seen(123) == 456

    sm.update_last_seen(123, 789)
    assert sm.get_last_seen(123) == 789


# =========================================================================
# Message history
# =========================================================================


def test_insert_and_get_messages(tmp_path):
    sm = _make_sm(tmp_path)
    sm.insert_message(
        message_id=1001, channel_id=123, guild_id=1,
        author_id=42, author_nickname="alice", is_bot=False,
        content="hello", timestamp=1000,
    )
    sm.insert_message(
        message_id=1002, channel_id=123, guild_id=1,
        author_id=43, author_nickname="bob", is_bot=False,
        content="world", timestamp=1001,
    )

    msgs = sm.get_recent_messages(123, limit=10)
    assert len(msgs) == 2
    assert msgs[0]["author"] == "alice"
    assert msgs[1]["author"] == "bob"


def test_insert_message_ignores_duplicate(tmp_path):
    sm = _make_sm(tmp_path)
    sm.insert_message(
        message_id=1001, channel_id=123, guild_id=1,
        author_id=42, author_nickname="alice", is_bot=False,
        content="original", timestamp=1000,
    )
    sm.insert_message(
        message_id=1001, channel_id=123, guild_id=1,
        author_id=42, author_nickname="alice", is_bot=False,
        content="duplicate", timestamp=1000,
    )

    msgs = sm.get_recent_messages(123)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "original"


def test_insert_message_webhook(tmp_path):
    sm = _make_sm(tmp_path)
    sm.insert_message(
        message_id=1001, channel_id=123, guild_id=1,
        author_id=42, author_nickname="webhook-bot", is_bot=True,
        content="webhook msg", timestamp=1000, is_webhook=True,
    )
    # Just verifying it doesn't crash -- is_webhook is stored but not returned by get_recent_messages


def test_update_message_content(tmp_path):
    sm = _make_sm(tmp_path)
    sm.insert_message(
        message_id=1001, channel_id=123, guild_id=1,
        author_id=42, author_nickname="alice", is_bot=False,
        content="original", timestamp=1000,
    )
    sm.update_message_content(1001, "edited")

    msgs = sm.get_recent_messages(123)
    assert msgs[0]["content"] == "edited"


def test_delete_messages(tmp_path):
    sm = _make_sm(tmp_path)
    for i in range(3):
        sm.insert_message(
            message_id=1000 + i, channel_id=123, guild_id=1,
            author_id=42, author_nickname="alice", is_bot=False,
            content=f"msg{i}", timestamp=1000 + i,
        )

    sm.delete_messages([1000, 1002])
    msgs = sm.get_recent_messages(123)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "msg1"


def test_delete_messages_empty_list(tmp_path):
    sm = _make_sm(tmp_path)
    sm.delete_messages([])  # should not crash


# =========================================================================
# Notifications
# =========================================================================


def test_add_and_get_notifications(tmp_path):
    sm = _make_sm(tmp_path)
    nid = sm.add_notification(
        type="task_complete", source="orchestrator",
        title="Task done", channel_id=123,
        payload={"task_id": "abc"},
    )
    assert nid > 0

    wendy_notifs = sm.get_unseen_notifications_for_wendy()
    assert len(wendy_notifs) == 1
    assert wendy_notifs[0].title == "Task done"
    assert wendy_notifs[0].payload == {"task_id": "abc"}
    assert wendy_notifs[0].seen_by_wendy is False

    proxy_notifs = sm.get_unseen_notifications_for_proxy()
    assert len(proxy_notifs) == 1


def test_mark_notifications_seen(tmp_path):
    sm = _make_sm(tmp_path)
    nid = sm.add_notification(type="test", source="test", title="test")

    sm.mark_notifications_seen_by_wendy([nid])
    assert len(sm.get_unseen_notifications_for_wendy()) == 0
    assert len(sm.get_unseen_notifications_for_proxy()) == 1

    sm.mark_notifications_seen_by_proxy([nid])
    assert len(sm.get_unseen_notifications_for_proxy()) == 0


def test_mark_notifications_seen_empty_list(tmp_path):
    sm = _make_sm(tmp_path)
    sm.mark_notifications_seen_by_wendy([])  # should not crash
    sm.mark_notifications_seen_by_proxy([])


def test_cleanup_old_notifications(tmp_path):
    sm = _make_sm(tmp_path)
    for i in range(5):
        sm.add_notification(type="test", source="test", title=f"notif-{i}")

    sm.cleanup_old_notifications(keep_count=2)
    # Should have at most 2 remaining
    conn = sm._get_conn()
    count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert count == 2


# =========================================================================
# Thread registry
# =========================================================================


def test_register_and_get_thread(tmp_path):
    sm = _make_sm(tmp_path)
    sm.register_thread(thread_id=999, parent_channel_id=123, folder_name="general_t_999")

    assert sm.get_thread_folder(999) == "general_t_999"
    assert sm.get_thread_parent(999) == 123


def test_get_thread_missing(tmp_path):
    sm = _make_sm(tmp_path)
    assert sm.get_thread_folder(999) is None
    assert sm.get_thread_parent(999) is None


# =========================================================================
# Session history
# =========================================================================


def test_session_archived_on_replace(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "sess-1", "general")
    sm.update_session_stats(123, input_tokens=50, output_tokens=25)

    # Creating a new session should archive the old one
    sm.create_session(123, "sess-2", "general")

    history = sm.get_session_history(123)
    assert len(history) == 1
    assert history[0]["session_id"] == "sess-1"
    assert history[0]["message_count"] == 1
    assert history[0]["total_input_tokens"] == 50


def test_session_not_archived_on_first_create(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "sess-1", "general")

    history = sm.get_session_history(123)
    assert len(history) == 0


def test_get_session_by_id_exact(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "aaaa-bbbb-cccc", "general")
    sm.create_session(123, "dddd-eeee-ffff", "general")  # archives first

    result = sm.get_session_by_id("aaaa-bbbb-cccc")
    assert result is not None
    assert result["session_id"] == "aaaa-bbbb-cccc"


def test_get_session_by_id_prefix(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "aaaa-bbbb-cccc", "general")
    sm.create_session(123, "dddd-eeee-ffff", "general")

    result = sm.get_session_by_id("aaaa")
    assert result is not None
    assert result["session_id"] == "aaaa-bbbb-cccc"


def test_get_session_by_id_active_session(tmp_path):
    sm = _make_sm(tmp_path)
    sm.create_session(123, "active-session-id", "general")

    result = sm.get_session_by_id("active")
    assert result is not None
    assert result["session_id"] == "active-session-id"


def test_get_session_by_id_not_found(tmp_path):
    sm = _make_sm(tmp_path)
    assert sm.get_session_by_id("nonexistent") is None


# =========================================================================
# Usage state
# =========================================================================


def test_usage_state(tmp_path):
    sm = _make_sm(tmp_path)
    assert sm.get_usage_threshold("test_key") == 0

    sm.set_usage_threshold("test_key", 42)
    assert sm.get_usage_threshold("test_key") == 42

    sm.set_usage_threshold("test_key", 100)
    assert sm.get_usage_threshold("test_key") == 100
