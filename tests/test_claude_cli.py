"""Unit tests for bot/claude_cli.py functions."""

import json
import pytest

from bot.claude_cli import (
    _count_discord_messages_in_tool_result,
    _count_discord_messages,
    ClaudeCliTextGenerator,
)


class TestCountDiscordMessagesInToolResult:
    """Tests for _count_discord_messages_in_tool_result."""

    def test_valid_messages_array(self):
        """Should count messages in valid check_messages response."""
        messages = [
            {"message_id": 123, "author": "user1", "content": "hello"},
            {"message_id": 124, "author": "user2", "content": "hi"},
            {"message_id": 125, "author": "user1", "content": "how are you"},
        ]
        result = _count_discord_messages_in_tool_result(json.dumps(messages))
        assert result == 3

    def test_empty_array(self):
        """Should return 0 for empty array."""
        result = _count_discord_messages_in_tool_result("[]")
        assert result == 0

    def test_invalid_json(self):
        """Should return 0 for invalid JSON."""
        result = _count_discord_messages_in_tool_result("not json")
        assert result == 0

    def test_non_message_array(self):
        """Should return 0 for array without message_id/author."""
        result = _count_discord_messages_in_tool_result('[{"foo": "bar"}]')
        assert result == 0

    def test_non_array_json(self):
        """Should return 0 for non-array JSON."""
        result = _count_discord_messages_in_tool_result('{"key": "value"}')
        assert result == 0

    def test_single_message(self):
        """Should count single message correctly."""
        messages = [{"message_id": 1, "author": "test", "content": "hi"}]
        result = _count_discord_messages_in_tool_result(json.dumps(messages))
        assert result == 1


class TestCountDiscordMessages:
    """Tests for _count_discord_messages."""

    def test_empty_messages(self):
        """Should return 0 for empty list."""
        assert _count_discord_messages([]) == 0

    def test_no_user_messages(self):
        """Should return 0 when no user messages."""
        messages = [
            {"type": "assistant", "message": {"content": "hi"}},
        ]
        assert _count_discord_messages(messages) == 0

    def test_user_message_with_tool_result(self):
        """Should count Discord messages in tool_result."""
        discord_msgs = [
            {"message_id": 1, "author": "user", "content": "hello"},
            {"message_id": 2, "author": "user", "content": "world"},
        ]
        messages = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": json.dumps(discord_msgs)}
                    ]
                }
            }
        ]
        assert _count_discord_messages(messages) == 2

    def test_multiple_tool_results(self):
        """Should sum across multiple tool results."""
        msgs1 = [{"message_id": 1, "author": "a", "content": "x"}]
        msgs2 = [
            {"message_id": 2, "author": "b", "content": "y"},
            {"message_id": 3, "author": "c", "content": "z"},
        ]
        messages = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": json.dumps(msgs1)},
                        {"type": "tool_result", "content": json.dumps(msgs2)},
                    ]
                }
            }
        ]
        assert _count_discord_messages(messages) == 3

    def test_non_list_content(self):
        """Should handle non-list content gracefully."""
        messages = [
            {"type": "user", "message": {"content": "string content"}}
        ]
        assert _count_discord_messages(messages) == 0


class TestGetPermissionsForChannel:
    """Tests for _get_permissions_for_channel."""

    def setup_method(self):
        """Create generator instance for testing."""
        # Mock the CLI path check
        import os
        os.environ["CLAUDE_CLI_PATH"] = "/bin/true"
        self.generator = ClaudeCliTextGenerator()

    def test_chat_mode_permissions(self):
        """Chat mode should have restricted permissions."""
        config = {"mode": "chat", "folder": "chat"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Should allow Read, WebSearch, WebFetch, Bash, and folder-specific Edit/Write
        assert "Read" in allowed
        assert "WebSearch" in allowed
        assert "Bash" in allowed
        assert "Edit(//data/wendy/chat/**)" in allowed
        assert "Write(//data/wendy/chat/**)" in allowed

        # Should disallow coding folder access
        assert "coding" in disallowed

    def test_full_mode_permissions(self):
        """Full mode should have broader permissions."""
        config = {"mode": "full", "folder": "coding"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Should allow uploads folder
        assert "Write(//data/wendy/uploads/**)" in allowed

        # Should not restrict coding folder
        assert "coding" not in disallowed

    def test_default_mode_is_full(self):
        """Missing mode should default to full."""
        config = {"folder": "wendys_folder"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Default should be full mode (allows uploads)
        assert "Write(//data/wendy/uploads/**)" in allowed

    def test_default_folder(self):
        """Missing folder should default to wendys_folder."""
        config = {"mode": "chat"}
        allowed, _ = self.generator._get_permissions_for_channel(config)

        assert "wendys_folder" in allowed
