"""Tests for wendy.cli."""
from __future__ import annotations

from wendy.cli import (
    build_cli_command,
    build_nudge_prompt,
    extract_forked_session_id,
    get_permissions_for_channel,
)

# =========================================================================
# Permissions
# =========================================================================


def test_get_permissions_full_mode():
    config = {"mode": "full", "_folder": "coding", "name": "coding"}
    allowed, disallowed = get_permissions_for_channel(config)
    assert "Read" in allowed
    assert "WebSearch" in allowed
    assert "Bash" in allowed
    assert "/data/wendy/channels/coding/" in allowed
    assert "/app/" in disallowed


def test_get_permissions_chat_mode():
    config = {"mode": "chat", "_folder": "chat", "name": "chat"}
    allowed, disallowed = get_permissions_for_channel(config)
    assert "Read" in allowed
    assert "/data/wendy/channels/chat/" in allowed


def test_get_permissions_uses_folder():
    config = {"mode": "full", "_folder": "custom_folder", "name": "original"}
    allowed, _ = get_permissions_for_channel(config)
    assert "custom_folder" in allowed
    assert "original" not in allowed


# =========================================================================
# CLI command building
# =========================================================================


def test_build_cli_command_new_session():
    cmd = build_cli_command(
        cli_path="/usr/bin/claude",
        session_id="abc-123",
        is_new_session=True,
        system_prompt="test prompt",
        channel_config={"mode": "full", "_folder": "coding"},
        model="claude-sonnet-4-5-20250929",
    )
    assert "/usr/bin/claude" in cmd
    assert "-p" in cmd
    assert "--session-id" in cmd
    assert "abc-123" in cmd
    assert "--model" in cmd
    assert "claude-sonnet-4-5-20250929" in cmd
    assert "--append-system-prompt" in cmd
    assert "--resume" not in cmd


def test_build_cli_command_resume():
    cmd = build_cli_command(
        cli_path="/usr/bin/claude",
        session_id="abc-123",
        is_new_session=False,
        system_prompt="test prompt",
        channel_config={"mode": "full", "_folder": "coding"},
        model="claude-sonnet-4-5-20250929",
    )
    assert "--resume" in cmd
    assert "abc-123" in cmd
    assert "--session-id" not in cmd


def test_build_cli_command_fork():
    cmd = build_cli_command(
        cli_path="/usr/bin/claude",
        session_id="abc-123",
        is_new_session=True,
        system_prompt="test prompt",
        channel_config={"mode": "full", "_folder": "coding"},
        model="claude-sonnet-4-5-20250929",
        fork_mode=True,
    )
    assert "--resume" in cmd
    assert "--fork-session" in cmd
    assert "--session-id" not in cmd


def test_build_cli_command_no_system_prompt():
    cmd = build_cli_command(
        cli_path="/usr/bin/claude",
        session_id="abc-123",
        is_new_session=True,
        system_prompt="",
        channel_config={"mode": "full", "_folder": "coding"},
        model="claude-sonnet-4-5-20250929",
    )
    assert "--append-system-prompt" not in cmd


# =========================================================================
# Nudge prompt
# =========================================================================


def test_build_nudge_prompt_normal():
    prompt = build_nudge_prompt(123)
    assert "msgs" in prompt
    assert "thread" not in prompt.lower()


def test_build_nudge_prompt_thread():
    prompt = build_nudge_prompt(456, is_thread=True, thread_name="cool-thread")
    assert "msgs" in prompt
    assert "cool-thread" in prompt
    assert "thread" in prompt.lower()


# =========================================================================
# Forked session ID extraction
# =========================================================================


def test_extract_forked_session_id_from_result():
    events = [
        {"type": "system", "session_id": "sys-id"},
        {"type": "assistant", "message": "hello"},
        {"type": "result", "session_id": "result-id"},
    ]
    assert extract_forked_session_id(events, "coding") == "result-id"


def test_extract_forked_session_id_from_system():
    events = [
        {"type": "system", "session_id": "sys-id"},
        {"type": "assistant", "message": "hello"},
    ]
    assert extract_forked_session_id(events, "coding") == "sys-id"


def test_extract_forked_session_id_none():
    events = [
        {"type": "assistant", "message": "hello"},
    ]
    assert extract_forked_session_id(events, "coding") is None
