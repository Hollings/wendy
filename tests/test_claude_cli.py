"""Unit tests for wendy.cli permission helpers.

Ported from bot.claude_cli tests.  The old ClaudeCliTextGenerator class
and _count_discord_messages* helpers were removed in the v2 rewrite.
Only get_permissions_for_channel (formerly a method) survives.
"""

from wendy.cli import get_permissions_for_channel


class TestGetPermissionsForChannel:
    """Tests for get_permissions_for_channel."""

    def test_allowed_tools_format(self):
        """Allowed string should contain the standard tools and channel paths."""
        config = {"mode": "full", "name": "coding"}
        allowed, disallowed = get_permissions_for_channel(config)

        assert "Read" in allowed
        assert "WebSearch" in allowed
        assert "WebFetch" in allowed
        assert "Bash" in allowed
        assert "Edit(//data/wendy/channels/coding/**)" in allowed
        assert "Write(//data/wendy/channels/coding/**)" in allowed

    def test_disallowed_tools_block_app_directory(self):
        """Both modes should block the app directory."""
        config = {"mode": "full", "name": "coding"}
        _, disallowed = get_permissions_for_channel(config)

        assert "Edit(//app/**)" in disallowed
        assert "Write(//app/**)" in disallowed

    def test_allows_fragment_people_access(self):
        """Should allow editing people fragment files."""
        config = {"mode": "full", "name": "coding"}
        allowed, _ = get_permissions_for_channel(config)

        assert "Edit(//data/wendy/claude_fragments/people/**)" in allowed
        assert "Write(//data/wendy/claude_fragments/people/**)" in allowed

    def test_missing_name_defaults_to_default(self):
        """Missing name should use 'default' in paths."""
        config = {"mode": "full"}
        allowed, _ = get_permissions_for_channel(config)

        assert "Edit(//data/wendy/channels/default/**)" in allowed
        assert "Write(//data/wendy/channels/default/**)" in allowed

    def test_folder_key_overrides_name(self):
        """_folder key should take precedence over name."""
        config = {"mode": "full", "name": "display_name", "_folder": "actual_folder"}
        allowed, _ = get_permissions_for_channel(config)

        assert "Edit(//data/wendy/channels/actual_folder/**)" in allowed
        assert "Write(//data/wendy/channels/actual_folder/**)" in allowed
        assert "display_name" not in allowed
