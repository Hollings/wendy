"""Tests for laurel reaction tracking (wendy.state) and rendering (wendy.laurels)."""
from __future__ import annotations

import time

from wendy import laurels
from wendy.laurels import get_laurels_section
from wendy.state import StateManager

CHANNEL = 111
NOW = int(time.time())
LONG_AGO = NOW - 365 * 86400


def _make_sm(tmp_path) -> StateManager:
    sm = StateManager(db_path=tmp_path / "test.db")
    sm._get_conn()
    return sm


def _insert_bot_message(sm: StateManager, message_id: int, content: str = "hello world") -> None:
    sm.insert_message(
        message_id=message_id, channel_id=CHANNEL, guild_id=1,
        author_id=42, author_nickname="Wendy", is_bot=True,
        content=content, timestamp=NOW,
    )


def _react(sm: StateManager, message_id: int, emoji: str, user_id: int, name: str | None = None) -> None:
    sm.add_laurel_reaction(
        message_id=message_id, channel_id=CHANNEL, emoji=emoji,
        user_id=user_id, user_name=name or f"user{user_id}",
    )


# =========================================================================
# State layer
# =========================================================================


def test_reaction_readd_is_idempotent(tmp_path):
    sm = _make_sm(tmp_path)
    _react(sm, 1001, "star", 1)
    _react(sm, 1001, "star", 1)

    rows = sm.get_laurels([CHANNEL], threshold=1, since_ts=0)
    assert len(rows) == 1
    assert rows[0]["count"] == 1


def test_threshold_gates_laurels(tmp_path):
    sm = _make_sm(tmp_path)
    _react(sm, 1001, "star", 1)
    _react(sm, 1001, "star", 2)

    assert sm.get_laurels([CHANNEL], threshold=3, since_ts=0) == []

    _react(sm, 1001, "star", 3)
    rows = sm.get_laurels([CHANNEL], threshold=3, since_ts=0)
    assert len(rows) == 1
    assert rows[0]["count"] == 3
    assert "user1" in rows[0]["reactors"]


def test_mixed_emojis_do_not_pool_toward_threshold(tmp_path):
    sm = _make_sm(tmp_path)
    _react(sm, 1001, "star", 1)
    _react(sm, 1001, "fire", 2)
    _react(sm, 1001, "heart", 3)

    assert sm.get_laurels([CHANNEL], threshold=3, since_ts=0) == []


def test_remove_reaction_drops_below_threshold(tmp_path):
    sm = _make_sm(tmp_path)
    for uid in (1, 2, 3):
        _react(sm, 1001, "star", uid)
    assert len(sm.get_laurels([CHANNEL], threshold=3, since_ts=0)) == 1

    sm.remove_laurel_reaction(1001, "star", 2)
    assert sm.get_laurels([CHANNEL], threshold=3, since_ts=0) == []


def test_remove_untracked_reaction_is_noop(tmp_path):
    sm = _make_sm(tmp_path)
    sm.remove_laurel_reaction(9999, "star", 1)  # must not raise


def test_clear_reactions(tmp_path):
    sm = _make_sm(tmp_path)
    for uid in (1, 2, 3):
        _react(sm, 1001, "star", uid)
        _react(sm, 1001, "fire", uid)

    sm.clear_laurel_reactions(1001, emoji="star")
    rows = sm.get_laurels([CHANNEL], threshold=3, since_ts=0)
    assert [r["emoji"] for r in rows] == ["fire"]

    sm.clear_laurel_reactions(1001)
    assert sm.get_laurels([CHANNEL], threshold=1, since_ts=0) == []


def test_window_excludes_old_reactions(tmp_path):
    sm = _make_sm(tmp_path)
    for uid in (1, 2, 3):
        _react(sm, 1001, "star", uid)
    # Backdate all rows beyond the window.
    conn = sm._get_conn()
    conn.execute("UPDATE laurel_reactions SET created_at = ?", (LONG_AGO,))
    conn.commit()

    assert sm.get_laurels([CHANNEL], threshold=3, since_ts=NOW - 86400) == []
    assert len(sm.get_laurels([CHANNEL], threshold=3, since_ts=0)) == 1


def test_channel_scoping(tmp_path):
    sm = _make_sm(tmp_path)
    for uid in (1, 2, 3):
        _react(sm, 1001, "star", uid)

    assert sm.get_laurels([222], threshold=3, since_ts=0) == []
    assert len(sm.get_laurels([222, CHANNEL], threshold=3, since_ts=0)) == 1


def test_get_laurels_joins_message_content(tmp_path):
    sm = _make_sm(tmp_path)
    _insert_bot_message(sm, 1001, "a truly great post")
    for uid in (1, 2, 3):
        _react(sm, 1001, "star", uid)

    rows = sm.get_laurels([CHANNEL], threshold=3, since_ts=0)
    assert rows[0]["content"] == "a truly great post"
    assert rows[0]["message_ts"] == NOW


def test_get_message_author(tmp_path):
    sm = _make_sm(tmp_path)
    _insert_bot_message(sm, 1001)
    assert sm.get_message_author(1001) == 42
    assert sm.get_message_author(9999) is None


# =========================================================================
# Rendering layer
# =========================================================================


def test_section_empty_when_no_laurels(tmp_path, monkeypatch):
    monkeypatch.setattr(laurels, "LAUREL_THRESHOLD", 3)
    sm = _make_sm(tmp_path)
    assert get_laurels_section([CHANNEL], sm=sm) == ""
    # Below threshold is also empty.
    _react(sm, 1001, "star", 1)
    assert get_laurels_section([CHANNEL], sm=sm) == ""


def test_section_renders_laurel(tmp_path, monkeypatch):
    monkeypatch.setattr(laurels, "LAUREL_THRESHOLD", 3)
    sm = _make_sm(tmp_path)
    _insert_bot_message(sm, 1001, "a truly great post")
    for uid, name in ((1, "alice"), (2, "bob"), (3, "carol")):
        _react(sm, 1001, "star", uid, name)

    section = get_laurels_section([CHANNEL], sm=sm)
    assert "LAURELS:" in section
    assert '"a truly great post"' in section
    assert "star x3" in section
    assert "alice" in section
    assert "nothing to do" in section


def test_section_collapses_multiple_emojis_per_message(tmp_path, monkeypatch):
    monkeypatch.setattr(laurels, "LAUREL_THRESHOLD", 2)
    sm = _make_sm(tmp_path)
    _insert_bot_message(sm, 1001, "double winner")
    for uid in (1, 2):
        _react(sm, 1001, "star", uid)
        _react(sm, 1001, "fire", uid)

    section = get_laurels_section([CHANNEL], sm=sm)
    assert section.count("double winner") == 1
    assert "star x2" in section
    assert "fire x2" in section


def test_section_truncates_long_content(tmp_path, monkeypatch):
    monkeypatch.setattr(laurels, "LAUREL_THRESHOLD", 1)
    sm = _make_sm(tmp_path)
    _insert_bot_message(sm, 1001, "x" * 500)
    _react(sm, 1001, "star", 1)

    section = get_laurels_section([CHANNEL], sm=sm)
    assert "x" * 117 + "..." in section
    assert "x" * 200 not in section


def test_section_handles_untracked_message_content(tmp_path, monkeypatch):
    """A laurel on a post that predates message caching still renders."""
    monkeypatch.setattr(laurels, "LAUREL_THRESHOLD", 1)
    sm = _make_sm(tmp_path)
    _react(sm, 1001, "star", 1)

    section = get_laurels_section([CHANNEL], sm=sm)
    assert "(no text" in section


def test_section_caps_shown_laurels(tmp_path, monkeypatch):
    monkeypatch.setattr(laurels, "LAUREL_THRESHOLD", 1)
    monkeypatch.setattr(laurels, "LAUREL_MAX_SHOWN", 2)
    sm = _make_sm(tmp_path)
    for mid in (1001, 1002, 1003):
        _insert_bot_message(sm, mid, f"post {mid}")
        _react(sm, mid, "star", 1)

    section = get_laurels_section([CHANNEL], sm=sm)
    bullets = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(bullets) == 2
