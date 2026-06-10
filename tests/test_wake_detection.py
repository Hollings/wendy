"""Tests for wake/pending-message detection (wendy.state).

These guard against the spurious-wake bugs where Wendy got woken for
messages that on_message would never wake her for (ignored users, command
messages) or for messages she had already seen.
"""
from __future__ import annotations

from wendy.state import StateManager

BOT_ID = 999
SYNTH_THRESHOLD = 9_000_000_000_000_000_000


def _make_sm(tmp_path) -> StateManager:
    sm = StateManager(db_path=tmp_path / "test.db")
    sm._get_conn()
    return sm


def _msg(sm, msg_id, author_id=42, content="hello", author="alice", is_bot=False):
    sm.insert_message(
        message_id=msg_id, channel_id=123, guild_id=1,
        author_id=author_id, author_nickname=author, is_bot=is_bot,
        content=content, timestamp=1000 + msg_id,
    )


def _check_new(sm, ignored=()):
    return sm.check_for_new_messages(
        123, bot_user_id=BOT_ID, synthetic_id_threshold=SYNTH_THRESHOLD,
        max_limit=50, ignored_author_ids=ignored,
    )


# =========================================================================
# has_pending_messages
# =========================================================================


def test_pending_true_for_new_user_message(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001)
    assert sm.has_pending_messages(123, BOT_ID) is True


def test_pending_false_when_already_seen(tmp_path):
    sm = _make_sm(tmp_path)
    _msg(sm, 1001)
    sm.update_last_seen(123, 1001)
    assert sm.has_pending_messages(123, BOT_ID) is False


def test_pending_excludes_bots_own_messages(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001, author_id=BOT_ID, author="wendy", is_bot=True)
    assert sm.has_pending_messages(123, BOT_ID) is False


def test_pending_excludes_ignored_users(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001, author_id=777, author="oracle", is_bot=True)
    assert sm.has_pending_messages(123, BOT_ID, ignored_author_ids={777}) is False
    # Without the ignore list the same message counts.
    assert sm.has_pending_messages(123, BOT_ID) is True


def test_pending_mixed_ignored_and_real(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001, author_id=777, author="oracle")
    _msg(sm, 1002, author_id=42, author="alice")
    assert sm.has_pending_messages(123, BOT_ID, ignored_author_ids={777}) is True


def test_pending_excludes_command_messages(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001, content="!session")
    _msg(sm, 1002, content="-debug thing")
    _msg(sm, 1003, content="/analysis foo")
    assert sm.has_pending_messages(123, BOT_ID) is False


def test_pending_counts_synthetic_messages(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, SYNTH_THRESHOLD + 5, author_id=0, author="Task System",
         content="[Task System] background task done")
    assert sm.has_pending_messages(123, BOT_ID) is True


def test_pending_excludes_leaked_context_intros(tmp_path):
    # Context intros are injected at generation start; ones leaked by a
    # killed generation must not wake the bot (msgs hides them anyway).
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, SYNTH_THRESHOLD + 5, author_id=0, author="Context",
         content="<introduction: alice is a person who...>")
    assert sm.has_pending_messages(123, BOT_ID) is False


def test_pending_no_watermark_excludes_ignored(tmp_path):
    sm = _make_sm(tmp_path)
    _msg(sm, 1001, author_id=777, author="oracle")
    assert sm.has_pending_messages(123, BOT_ID, ignored_author_ids={777}) is False


# =========================================================================
# check_for_new_messages (startup catchup + send interrupt)
# =========================================================================


def test_check_new_returns_unseen_user_messages(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001, content="hey wendy")
    msgs = _check_new(sm)
    assert [m["content"] for m in msgs] == ["hey wendy"]


def test_check_new_excludes_ignored_users(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001, author_id=777, author="oracle", content="bot chatter")
    assert _check_new(sm, ignored={777}) == []
    assert len(_check_new(sm)) == 1


def test_check_new_excludes_own_messages(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001, author_id=BOT_ID, author="wendy", is_bot=True)
    assert _check_new(sm) == []


def test_check_new_no_watermark_returns_empty(tmp_path):
    # Fresh channel with no watermark: nothing is "unseen" yet.
    sm = _make_sm(tmp_path)
    _msg(sm, 1001)
    assert _check_new(sm) == []


def test_check_new_excludes_slash_commands(tmp_path):
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, 1001, content="/somecommand")
    assert _check_new(sm) == []


def test_check_new_excludes_synthetic_messages(tmp_path):
    # Catchup and the send interrupt are about real messages only --
    # notifications wake via their own path, and leaked Context intros
    # must not produce wakes where msgs then shows nothing.
    sm = _make_sm(tmp_path)
    sm.update_last_seen(123, 1000)
    _msg(sm, SYNTH_THRESHOLD + 5, author_id=0, author="Context",
         content="<introduction>")
    _msg(sm, SYNTH_THRESHOLD + 6, author_id=0, author="Task System",
         content="[Task System] task done")
    assert _check_new(sm) == []
