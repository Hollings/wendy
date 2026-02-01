"""Unit tests for bot/claude_cli.py functions."""

import json

from bot.claude_cli import (
    ClaudeCliTextGenerator,
    _count_discord_messages,
    _count_discord_messages_in_tool_result,
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

    def test_multiple_tool_results_across_messages(self):
        """Should sum Discord messages across multiple user messages."""
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
                    ]
                }
            },
            {"type": "assistant", "message": {"content": "ok"}},
            {
                "type": "user",
                "message": {
                    "content": [
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

    def test_mixed_tool_results_only_counts_discord(self):
        """Should only count tool_results that look like Discord messages."""
        discord_msgs = [{"message_id": 1, "author": "user", "content": "hi"}]
        messages = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "some text output"},
                        {"type": "tool_result", "content": json.dumps(discord_msgs)},
                        {"type": "tool_result", "content": '{"not": "a message list"}'},
                    ]
                }
            }
        ]
        assert _count_discord_messages(messages) == 1


class TestGetPermissionsForChannel:
    """Tests for _get_permissions_for_channel."""

    def setup_method(self):
        """Create generator instance for testing."""
        import os
        os.environ["CLAUDE_CLI_PATH"] = "/bin/true"
        self.generator = ClaudeCliTextGenerator()

    def test_chat_mode_exact_allowed_tools(self):
        """Chat mode allowed string should have exact expected format."""
        config = {"mode": "chat", "name": "mychat"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Verify the exact allowed tools list
        expected_allowed = (
            "Read,WebSearch,WebFetch,Bash,"
            "Edit(//data/wendy/channels/mychat/**),"
            "Write(//data/wendy/channels/mychat/**),"
            "Write(//data/wendy/tmp/**),"
            "Write(//tmp/**)"
        )
        assert allowed == expected_allowed

    def test_chat_mode_disallowed_tools(self):
        """Chat mode should block editing scripts and app directory."""
        config = {"mode": "chat", "name": "mychat"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Chat mode blocks editing shell scripts, python scripts, and app dir
        assert "Edit(//data/wendy/*.sh)" in disallowed
        assert "Edit(//data/wendy/*.py)" in disallowed
        assert "Edit(//app/**)" in disallowed
        assert "Write(//app/**)" in disallowed

    def test_full_mode_exact_allowed_tools(self):
        """Full mode allowed string should have exact expected format."""
        config = {"mode": "full", "name": "coding"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        expected_allowed = (
            "Read,WebSearch,WebFetch,Bash,"
            "Edit(//data/wendy/channels/coding/**),"
            "Write(//data/wendy/channels/coding/**),"
            "Write(//data/wendy/tmp/**),"
            "Write(//tmp/**)"
        )
        assert allowed == expected_allowed

    def test_full_mode_disallowed_tools(self):
        """Full mode should only block app directory."""
        config = {"mode": "full", "name": "coding"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Full mode only blocks app dir
        assert disallowed == "Edit(//app/**),Write(//app/**)"

    def test_missing_mode_defaults_to_full(self):
        """Missing mode should default to full mode permissions."""
        config = {"name": "testchan"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Should get full mode permissions (only app blocked)
        assert disallowed == "Edit(//app/**),Write(//app/**)"
        assert "Edit(//data/wendy/channels/testchan/**)" in allowed

    def test_missing_name_defaults_to_default(self):
        """Missing name should use 'default' in paths."""
        config = {"mode": "full"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Should use "default" as channel name in paths
        assert "Edit(//data/wendy/channels/default/**)" in allowed
        assert "Write(//data/wendy/channels/default/**)" in allowed

    def test_folder_key_overrides_name(self):
        """_folder key should take precedence over name for backwards compat."""
        config = {"mode": "full", "name": "display_name", "_folder": "actual_folder"}
        allowed, disallowed = self.generator._get_permissions_for_channel(config)

        # Should use _folder, not name
        assert "Edit(//data/wendy/channels/actual_folder/**)" in allowed
        assert "Write(//data/wendy/channels/actual_folder/**)" in allowed
        assert "display_name" not in allowed
