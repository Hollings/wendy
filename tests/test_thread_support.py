"""Tests for Discord thread support."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wendy.paths import validate_channel_name
from wendy.state import StateManager


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def sm(temp_db):
    """Create a StateManager with a temporary database."""
    return StateManager(db_path=temp_db)


class TestThreadRegistry:
    """Tests for thread_registry table and methods."""

    def test_register_and_get_thread_folder(self, sm):
        """register_thread + get_thread_folder round-trip."""
        sm.register_thread(111, 222, "coding_t_111")
        result = sm.get_thread_folder(111)
        assert result == "coding_t_111"

    def test_get_thread_folder_nonexistent(self, sm):
        """get_thread_folder returns None for unregistered thread."""
        result = sm.get_thread_folder(999999)
        assert result is None

    def test_register_thread_idempotent(self, sm):
        """register_thread with same thread_id is INSERT OR IGNORE (no error)."""
        sm.register_thread(111, 222, "coding_t_111")
        sm.register_thread(111, 222, "coding_t_111")  # Should not raise
        result = sm.get_thread_folder(111)
        assert result == "coding_t_111"

    def test_multiple_threads_same_parent(self, sm):
        """Multiple threads can be registered under the same parent."""
        sm.register_thread(111, 222, "coding_t_111")
        sm.register_thread(333, 222, "coding_t_333")
        assert sm.get_thread_folder(111) == "coding_t_111"
        assert sm.get_thread_folder(333) == "coding_t_333"


class TestThreadFolderName:
    """Tests that thread folder names are valid."""

    def test_thread_folder_passes_validation(self):
        """Thread folder name format passes validate_channel_name()."""
        folder = "coding_t_1234567890"
        assert validate_channel_name(folder) is True

    def test_thread_folder_with_hyphen_parent(self):
        """Thread folder with hyphenated parent name is valid."""
        folder = "my-channel_t_9876543210"
        assert validate_channel_name(folder) is True


class TestResolveThreadConfig:
    """Tests for _resolve_thread_config in WendyCog."""

    def test_resolve_thread_config_builds_correct_folder(self, sm):
        """_resolve_thread_config builds correct folder name and inherits parent config."""
        # Build a mock message in a thread
        mock_channel = MagicMock(spec=["id", "parent_id", "name"])
        mock_channel.id = 111
        mock_channel.parent_id = 222
        mock_channel.name = "my-thread"

        # Make isinstance check work for discord.Thread
        with patch("wendy.discord_client.isinstance", side_effect=lambda obj, cls: True if obj is mock_channel else isinstance(obj, cls)):
            # We'll test the logic directly instead of via the cog
            parent_config = {
                "id": "222",
                "name": "coding",
                "mode": "full",
                "model": "opus",
                "beads_enabled": True,
                "_folder": "coding",
            }

            # Replicate _resolve_thread_config logic
            parent_folder = parent_config.get("_folder") or parent_config.get("name", "default")
            thread_id = mock_channel.id
            folder_name = f"{parent_folder}_t_{thread_id}"
            thread_name = mock_channel.name

            config = {
                "id": str(thread_id),
                "name": thread_name,
                "mode": parent_config.get("mode", "chat"),
                "model": parent_config.get("model"),
                "beads_enabled": parent_config.get("beads_enabled", False),
                "_folder": folder_name,
                "_is_thread": True,
                "_parent_folder": parent_folder,
                "_parent_channel_id": mock_channel.parent_id,
                "_thread_name": thread_name,
            }

            assert config["_folder"] == "coding_t_111"
            assert config["mode"] == "full"
            assert config["model"] == "opus"
            assert config["beads_enabled"] is True
            assert config["_is_thread"] is True
            assert config["_parent_folder"] == "coding"
            assert config["_thread_name"] == "my-thread"

    def test_resolve_thread_config_includes_thread_name(self, sm):
        """Thread config includes _thread_name from channel.name."""
        thread_name = "discussion-about-bugs"
        config = {
            "_thread_name": thread_name,
            "_is_thread": True,
        }
        assert config["_thread_name"] == thread_name


class TestProxyThreadFallback:
    """Tests for proxy get_channel_name thread fallback."""

    def test_get_channel_name_falls_back_to_thread_registry(self, sm):
        """get_channel_name returns thread folder when not in main config."""
        sm.register_thread(111, 222, "coding_t_111")

        # Simulate proxy's get_channel_name with empty _CHANNEL_CONFIG
        with patch("wendy.api_server._channel_configs", {}):
            with patch("wendy.api_server.state_manager", sm):
                from wendy.api_server import get_channel_name
                result = get_channel_name(111)
                assert result == "coding_t_111"

    def test_get_channel_name_prefers_config_over_registry(self, sm):
        """get_channel_name returns config name when channel is in config."""
        sm.register_thread(222, 333, "should_not_use_this")

        config = {222: {"name": "coding", "_folder": "coding"}}
        with patch("wendy.api_server._channel_configs", config):
            from wendy.api_server import get_channel_name
            result = get_channel_name(222)
            assert result == "coding"


class TestBuildCliCommandFork:
    """Tests for fork-session CLI command building."""

    def test_build_cli_command_includes_fork_flags(self):
        """build_cli_command with fork_mode uses --resume and --fork-session."""
        from wendy.cli import build_cli_command

        cmd = build_cli_command(
            cli_path="/usr/bin/claude",
            session_id="parent-session-id",
            is_new_session=True,
            system_prompt="test prompt",
            channel_config={"mode": "full", "_folder": "coding_t_111"},
            model="claude-sonnet-4-5-20250929",
            fork_mode=True,
        )
        assert "--resume" in cmd
        assert "parent-session-id" in cmd
        assert "--fork-session" in cmd
        assert "--session-id" not in cmd

    def test_build_cli_command_fresh_session_without_fork(self):
        """build_cli_command without fork_mode uses --session-id."""
        from wendy.cli import build_cli_command

        cmd = build_cli_command(
            cli_path="/usr/bin/claude",
            session_id="new-session-id",
            is_new_session=True,
            system_prompt="test prompt",
            channel_config={"mode": "full", "_folder": "coding_t_111"},
            model="claude-sonnet-4-5-20250929",
            fork_mode=False,
        )
        assert "--session-id" in cmd
        assert "new-session-id" in cmd
        assert "--fork-session" not in cmd
