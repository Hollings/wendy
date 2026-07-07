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
# Synthetic-message delivery lifecycle (crash-safe consumption)
# =========================================================================

SYNTH = 9_000_000_000_000_000_000


def _insert_real(sm, mid, channel_id=123, author_id=42, content="hi", ts=None):
    sm.insert_message(
        message_id=mid, channel_id=channel_id, guild_id=1,
        author_id=author_id, author_nickname="alice", is_bot=False,
        content=content, timestamp=ts if ts is not None else mid,
    )


def _insert_synth(sm, mid, channel_id=123, content="[System] note"):
    sm.insert_message(
        message_id=mid, channel_id=channel_id, guild_id=None,
        author_id=0, author_nickname="System", is_bot=False,
        content=content, timestamp=mid,
    )


def _fetch_ids(sm, channel_id=123, since_id=None):
    rows = sm.fetch_messages(channel_id, since_id=since_id, limit=50)
    return {r["message_id"] for r in rows}


def test_fetch_includes_real_and_undelivered_synthetics(tmp_path):
    sm = _make_sm(tmp_path)
    _insert_real(sm, 1001)
    _insert_synth(sm, SYNTH + 5)

    ids = _fetch_ids(sm)
    assert ids == {1001, SYNTH + 5}


def test_mark_delivered_excludes_synthetic_from_fetch(tmp_path):
    sm = _make_sm(tmp_path)
    _insert_real(sm, 1001)
    _insert_synth(sm, SYNTH + 5)

    sm.mark_synthetics_delivered([SYNTH + 5])
    # Real message still visible; delivered synthetic hidden.
    assert _fetch_ids(sm) == {1001}
    # Even with a since_id, the delivered synthetic stays hidden.
    assert _fetch_ids(sm, since_id=1000) == {1001}


def test_rollback_redelivers_synthetic(tmp_path):
    sm = _make_sm(tmp_path)
    _insert_synth(sm, SYNTH + 5)

    sm.mark_synthetics_delivered([SYNTH + 5])
    assert _fetch_ids(sm) == set()

    sm.rollback_delivered_synthetics(123)
    assert _fetch_ids(sm) == {SYNTH + 5}


def test_commit_deletes_only_delivered_synthetics(tmp_path):
    sm = _make_sm(tmp_path)
    _insert_synth(sm, SYNTH + 5)
    _insert_synth(sm, SYNTH + 6)
    _insert_real(sm, 1001)

    # Deliver only one of the two synthetics.
    sm.mark_synthetics_delivered([SYNTH + 5])
    sm.commit_delivered_synthetics(123)

    # Delivered synthetic is gone; undelivered one and the real message survive.
    ids = _fetch_ids(sm)
    assert ids == {SYNTH + 6, 1001}
    # And it is truly deleted from the DB, not just hidden.
    conn = sm._get_conn()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM message_history WHERE message_id = ?", (SYNTH + 5,)
    ).fetchone()[0]
    assert remaining == 0


def test_commit_and_rollback_are_channel_scoped(tmp_path):
    sm = _make_sm(tmp_path)
    _insert_synth(sm, SYNTH + 5, channel_id=123)
    _insert_synth(sm, SYNTH + 6, channel_id=456)
    sm.mark_synthetics_delivered([SYNTH + 5, SYNTH + 6])

    # Committing channel 123 must not touch channel 456's delivered synthetic.
    sm.commit_delivered_synthetics(123)
    conn = sm._get_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM message_history WHERE channel_id = 456"
    ).fetchone()[0] == 1


def test_mark_delivered_empty_list(tmp_path):
    sm = _make_sm(tmp_path)
    sm.mark_synthetics_delivered([])  # should not crash


# =========================================================================
# Unread real-message count (state-driven Stop hook)
# =========================================================================


def test_count_unread_real_messages(tmp_path):
    sm = _make_sm(tmp_path)
    ch, bot = 200, 99
    _insert_real(sm, 2001, channel_id=ch, author_id=1, content="hello")
    _insert_real(sm, 2002, channel_id=ch, author_id=bot, content="bot reply")
    _insert_real(sm, 2003, channel_id=ch, author_id=1, content="!command")
    _insert_real(sm, 2004, channel_id=ch, author_id=1, content="another")
    _insert_synth(sm, SYNTH + 1, channel_id=ch)

    # No watermark: counts non-bot, non-command, non-synthetic only (2001, 2004).
    assert sm.count_unread_real_messages(ch, bot) == 2

    # Watermark past the first message: only 2004 remains unread.
    sm.update_last_seen(ch, 2001)
    assert sm.count_unread_real_messages(ch, bot) == 1

    # Watermark past everything: nothing unread.
    sm.update_last_seen(ch, 2004)
    assert sm.count_unread_real_messages(ch, bot) == 0


def test_count_unread_empty_channel(tmp_path):
    sm = _make_sm(tmp_path)
    assert sm.count_unread_real_messages(999, 0) == 0


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


# =========================================================================
# fetch_messages -- unread bursts must not skip messages
# =========================================================================


def test_fetch_messages_unread_returns_oldest_first(tmp_path):
    """A burst larger than the limit returns the OLDEST messages so the
    watermark (max returned ID) never jumps past unreturned ones."""
    sm = _make_sm(tmp_path)
    for i in range(1, 16):  # 15 unread: ids 1001..1015
        _insert_real(sm, 1000 + i, channel_id=5, content=f"m{i}")

    rows = sm.fetch_messages(5, since_id=1000, limit=10)
    ids = [r["message_id"] for r in rows]
    # DESC contract preserved, but the batch is the oldest 10 (1001..1010)
    assert ids == list(range(1010, 1000, -1))

    # Advancing the watermark to the max returned ID leaves the rest readable.
    sm.update_last_seen(5, max(ids))
    rows2 = sm.fetch_messages(5, since_id=sm.get_last_seen(5), limit=10)
    assert sorted(r["message_id"] for r in rows2) == list(range(1011, 1016))


def test_fetch_messages_no_watermark_returns_newest(tmp_path):
    """Without a watermark (fresh channel) the newest N are still returned."""
    sm = _make_sm(tmp_path)
    for i in range(1, 16):
        _insert_real(sm, 2000 + i, channel_id=6)

    rows = sm.fetch_messages(6, since_id=None, limit=5)
    ids = [r["message_id"] for r in rows]
    assert ids == list(range(2015, 2010, -1))
