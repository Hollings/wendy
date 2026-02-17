"""Tests for wendy.sessions."""
from __future__ import annotations

import json
from unittest import mock

from wendy import sessions
from wendy.state import StateManager


def _setup_state(tmp_path):
    """Create a fresh StateManager and patch the sessions module to use it."""
    sm = StateManager(db_path=tmp_path / "test.db")
    sm._get_conn()
    return sm


def test_create_session(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        sid = sessions.create_session(123, "general")
        assert isinstance(sid, str)
        assert len(sid) > 0

        info = sm.get_session(123)
        assert info is not None
        assert info.session_id == sid
        assert info.folder == "general"


def test_create_session_with_explicit_id(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        sid = sessions.create_session(123, "general", session_id="custom-id")
        assert sid == "custom-id"

        info = sm.get_session(123)
        assert info.session_id == "custom-id"


def test_get_session(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        assert sessions.get_session(123) is None

        sessions.create_session(123, "general")
        info = sessions.get_session(123)
        assert info is not None
        assert info.folder == "general"


def test_reset_session(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        sessions.create_session(123, "general")
        new_sid = sessions.reset_session(123, "general")

        info = sm.get_session(123)
        assert info.session_id == new_sid
        assert info.message_count == 0


def test_update_stats(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        sessions.create_session(123, "general")

        # Mock truncate_if_needed since it accesses filesystem
        with mock.patch.object(sessions, "truncate_if_needed"):
            sessions.update_stats(123, {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
            })

        info = sm.get_session(123)
        assert info.message_count == 1
        assert info.total_input_tokens == 100
        assert info.total_output_tokens == 50
        assert info.total_cache_read_tokens == 10


def test_update_stats_no_session(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        # Should not crash when no session exists
        sessions.update_stats(999, {"input_tokens": 100})


# =========================================================================
# Message counting
# =========================================================================


def test_count_discord_messages_in_tool_result():
    content = json.dumps([
        {"message_id": 1, "author": "alice", "content": "hello"},
        {"message_id": 2, "author": "bob", "content": "world"},
    ])
    assert sessions._count_discord_messages_in_tool_result(content) == 2


def test_count_discord_messages_in_tool_result_not_messages():
    assert sessions._count_discord_messages_in_tool_result("not json") == 0
    assert sessions._count_discord_messages_in_tool_result("[]") == 0
    assert sessions._count_discord_messages_in_tool_result('[{"key": "val"}]') == 0


def test_count_discord_messages():
    messages = [
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": json.dumps([
                            {"message_id": 1, "author": "alice", "content": "hello"},
                            {"message_id": 2, "author": "bob", "content": "world"},
                        ]),
                    }
                ]
            }
        },
        {"type": "assistant", "message": {"content": "response"}},
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": json.dumps([
                            {"message_id": 3, "author": "charlie", "content": "hi"},
                        ]),
                    }
                ]
            }
        },
    ]
    assert sessions._count_discord_messages(messages) == 3


def test_count_discord_messages_empty():
    assert sessions._count_discord_messages([]) == 0


# =========================================================================
# Truncation
# =========================================================================


def test_truncate_if_needed_no_file(tmp_path):
    # Should not crash when session file doesn't exist
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    with mock.patch.object(sessions, "session_dir", return_value=sess_dir):
        sessions.truncate_if_needed("nonexistent-session", "general")


def test_truncate_if_needed_under_limit(tmp_path):
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    session_file = sess_dir / "test-session.jsonl"

    # Write a session with only a few messages (under MAX_DISCORD_MESSAGES)
    messages = [
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": json.dumps([{"message_id": i, "author": "a", "content": "x"}])}]}}
        for i in range(5)
    ]
    with open(session_file, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    with mock.patch.object(sessions, "session_dir", return_value=sess_dir):
        sessions.truncate_if_needed("test-session", "general")

    # File should remain unchanged
    with open(session_file) as f:
        lines = f.readlines()
    assert len(lines) == 5


def test_truncate_if_needed_over_limit(tmp_path):
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    session_file = sess_dir / "test-session.jsonl"

    # Write a session with many messages (over MAX_DISCORD_MESSAGES = 50)
    # Each "user" message contains 2 discord messages in its tool_result
    messages = []
    for i in range(60):
        messages.append({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": json.dumps([
                {"message_id": i * 2, "author": "a", "content": f"msg {i * 2}"},
                {"message_id": i * 2 + 1, "author": "b", "content": f"msg {i * 2 + 1}"},
            ])}]}
        })
        messages.append({"type": "assistant", "message": {"content": f"response {i}"}})

    with open(session_file, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    with mock.patch.object(sessions, "session_dir", return_value=sess_dir):
        sessions.truncate_if_needed("test-session", "general")

    # File should be shorter now
    with open(session_file) as f:
        lines = f.readlines()
    assert len(lines) < 120  # original was 120 lines (60 user + 60 assistant)
