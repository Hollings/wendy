"""Tests for wendy.sessions."""
from __future__ import annotations

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
        old_sid = sessions.create_session(123, "general")
        returned_old, new_sid = sessions.reset_session(123, "general")

        assert returned_old == old_sid
        info = sm.get_session(123)
        assert info.session_id == new_sid
        assert info.message_count == 0


def test_update_stats(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        sessions.create_session(123, "general")

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


def test_resume_session(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        sessions.create_session(123, "general")
        original_sid = sm.get_session(123).session_id

        sessions.resume_session(123, "explicit-session-id", "general")

        info = sm.get_session(123)
        assert info.session_id == "explicit-session-id"
        assert info.session_id != original_sid


def test_reset_session_archives_history(tmp_path):
    sm = _setup_state(tmp_path)
    with mock.patch.object(sessions, "state_manager", sm):
        old_sid = sessions.create_session(123, "general")
        sessions.reset_session(123, "general")

        history = sm.get_session_history(123)
        assert len(history) == 1
        assert history[0]["session_id"] == old_sid
