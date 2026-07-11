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
    prompt = build_nudge_prompt()
    assert "msgs" in prompt
    assert "thread" not in prompt.lower()


def test_build_nudge_prompt_thread():
    prompt = build_nudge_prompt(is_thread=True, thread_name="cool-thread")
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


# =========================================================================
# Overloaded-error detection precision
# =========================================================================


def test_jsonl_overloaded_ignores_conversation_content():
    """Conversation content quoting the literal string must not kill the CLI."""
    import json as _json

    from wendy.cli import _jsonl_line_is_overloaded

    user = _json.dumps({"type": "user", "message": {"content": "my log says overloaded_error"}})
    assert not _jsonl_line_is_overloaded(user)
    assistant = _json.dumps({"type": "assistant", "message": {"content": "overloaded_error is matched in cli.py"}})
    assert not _jsonl_line_is_overloaded(assistant)


def test_jsonl_overloaded_detects_api_error_entries():
    import json as _json

    from wendy.cli import _jsonl_line_is_overloaded

    api_err = _json.dumps({
        "type": "assistant", "isApiErrorMessage": True,
        "message": {"content": "API Error: 529 overloaded_error"},
    })
    assert _jsonl_line_is_overloaded(api_err)
    # Unknown non-conversation record types containing the string still count.
    other = _json.dumps({"type": "system", "detail": "overloaded_error"})
    assert _jsonl_line_is_overloaded(other)


def test_jsonl_overloaded_partial_or_plain_lines():
    from wendy.cli import _jsonl_line_is_overloaded

    assert not _jsonl_line_is_overloaded('{"type": "assistant", "overloaded_error')  # torn mid-write
    assert not _jsonl_line_is_overloaded("nothing to see here")


def test_stream_event_overloaded_precision():
    from wendy.cli import _stream_event_is_overloaded

    assert not _stream_event_is_overloaded({"type": "assistant", "message": {}})
    assert not _stream_event_is_overloaded({"type": "user"})
    assert not _stream_event_is_overloaded({"type": "result", "is_error": False})
    assert _stream_event_is_overloaded({"type": "result", "is_error": True})
    assert _stream_event_is_overloaded({"type": "assistant", "isApiErrorMessage": True})
