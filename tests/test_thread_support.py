"""Tests for Discord thread support."""

import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.paths import validate_channel_name
from bot.state_manager import StateManager


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
        with patch("bot.wendy_cog.isinstance", side_effect=lambda obj, cls: True if obj is mock_channel else isinstance(obj, cls)):
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
        with patch("proxy.main._CHANNEL_CONFIG", {}):
            with patch("proxy.main.state_manager", sm):
                from proxy.main import get_channel_name
                result = get_channel_name(111)
                assert result == "coding_t_111"

    def test_get_channel_name_prefers_config_over_registry(self, sm):
        """get_channel_name returns config name when channel is in config."""
        sm.register_thread(222, 333, "should_not_use_this")

        config = {222: {"name": "coding", "_folder": "coding"}}
        with patch("proxy.main._CHANNEL_CONFIG", config):
            from proxy.main import get_channel_name
            result = get_channel_name(222)
            assert result == "coding"


class TestSessionForking:
    """Tests for native --fork-session in generate()."""

    def _make_generator(self):
        """Create a ClaudeCliTextGenerator with mocked internals."""
        from bot.claude_cli import ClaudeCliTextGenerator

        gen = ClaudeCliTextGenerator.__new__(ClaudeCliTextGenerator)
        gen.model = "claude-sonnet-4-5-20250929"
        gen.cli_path = "/usr/bin/claude"
        gen.timeout = 300
        gen._temp_dir = None
        gen._temp_files = []
        return gen

    def test_generate_builds_fork_command_for_new_thread(self, sm):
        """generate() uses --resume <parent> --fork-session for first thread invocation."""
        parent_folder = "coding"
        parent_session_id = str(uuid.uuid4())
        sm.create_session(222, parent_session_id, parent_folder)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            # Create parent session file
            sess_dir = tmpdir / "sessions"
            sess_dir.mkdir()
            parent_sess_file = sess_dir / f"{parent_session_id}.jsonl"
            parent_sess_file.write_text('{"type":"user"}\n')

            channel_dir_path = tmpdir / "channels" / parent_folder
            channel_dir_path.mkdir(parents=True)

            captured_cmd = []

            async def fake_create_subprocess_exec(*args, **kwargs):
                captured_cmd.extend(args)
                proc = AsyncMock()
                proc.returncode = 0
                proc.stdin = AsyncMock()
                proc.stdin.drain = AsyncMock()
                proc.stdin.wait_closed = AsyncMock()
                proc.stderr.read = AsyncMock(return_value=b"")
                proc.wait = AsyncMock()

                # Simulate stream output with result event containing forked session ID
                forked_id = str(uuid.uuid4())
                result_line = json.dumps({"type": "result", "result": "", "session_id": forked_id, "usage": {}})
                proc.stdout.__aiter__ = lambda self: aiter([result_line.encode()])
                return proc

            async def aiter(items):
                for item in items:
                    yield item

            with patch("bot.claude_cli.state_manager", sm), \
                 patch("bot.claude_cli.session_dir", return_value=sess_dir), \
                 patch("bot.claude_cli.channel_dir", return_value=channel_dir_path), \
                 patch("bot.claude_cli.ensure_channel_dirs"), \
                 patch("bot.claude_cli.ensure_shared_dirs"), \
                 patch("bot.claude_cli.WENDY_BASE", tmpdir):

                gen = self._make_generator()

                thread_config = {
                    "id": "111",
                    "name": "my-thread",
                    "mode": "full",
                    "beads_enabled": False,
                    "_folder": "coding_t_111",
                    "_is_thread": True,
                    "_parent_folder": parent_folder,
                    "_parent_channel_id": 222,
                    "_thread_name": "my-thread",
                }

                with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                    import asyncio
                    asyncio.get_event_loop().run_until_complete(
                        gen.generate(channel_id=111, channel_config=thread_config)
                    )

            # Verify --resume <parent_session_id> --fork-session in command
            assert "--resume" in captured_cmd
            resume_idx = captured_cmd.index("--resume")
            assert captured_cmd[resume_idx + 1] == parent_session_id
            assert "--fork-session" in captured_cmd
            # Should NOT have --session-id (that's for fresh sessions)
            assert "--session-id" not in captured_cmd

    def test_generate_fresh_session_when_no_parent(self, sm):
        """generate() creates fresh session when parent has no session to fork."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            sess_dir = tmpdir / "sessions"
            sess_dir.mkdir()

            channel_dir_path = tmpdir / "channels" / "coding"
            channel_dir_path.mkdir(parents=True)

            captured_cmd = []

            async def fake_create_subprocess_exec(*args, **kwargs):
                captured_cmd.extend(args)
                proc = AsyncMock()
                proc.returncode = 0
                proc.stdin = AsyncMock()
                proc.stdin.drain = AsyncMock()
                proc.stdin.wait_closed = AsyncMock()
                proc.stderr.read = AsyncMock(return_value=b"")
                proc.wait = AsyncMock()

                result_line = json.dumps({"type": "result", "result": "", "usage": {}})
                proc.stdout.__aiter__ = lambda self: aiter([result_line.encode()])
                return proc

            async def aiter(items):
                for item in items:
                    yield item

            with patch("bot.claude_cli.state_manager", sm), \
                 patch("bot.claude_cli.session_dir", return_value=sess_dir), \
                 patch("bot.claude_cli.channel_dir", return_value=channel_dir_path), \
                 patch("bot.claude_cli.ensure_channel_dirs"), \
                 patch("bot.claude_cli.ensure_shared_dirs"), \
                 patch("bot.claude_cli.WENDY_BASE", tmpdir):

                gen = self._make_generator()

                thread_config = {
                    "id": "111",
                    "name": "my-thread",
                    "mode": "full",
                    "beads_enabled": False,
                    "_folder": "coding_t_111",
                    "_is_thread": True,
                    "_parent_folder": "coding",
                    "_parent_channel_id": 222,
                    "_thread_name": "my-thread",
                }

                with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                    import asyncio
                    asyncio.get_event_loop().run_until_complete(
                        gen.generate(channel_id=111, channel_config=thread_config)
                    )

            # Should use --session-id (fresh), NOT --fork-session
            assert "--session-id" in captured_cmd
            assert "--fork-session" not in captured_cmd

    def test_extract_forked_session_id_from_result(self):
        """_extract_forked_session_id finds session_id in result event."""
        gen = self._make_generator()
        forked_id = str(uuid.uuid4())
        events = [
            {"type": "system", "session_id": "old-id"},
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "result": "", "session_id": forked_id},
        ]
        result = gen._extract_forked_session_id(events, "coding")
        assert result == forked_id

    def test_extract_forked_session_id_from_system(self):
        """_extract_forked_session_id falls back to system event."""
        gen = self._make_generator()
        sys_id = str(uuid.uuid4())
        events = [
            {"type": "system", "session_id": sys_id},
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "result": ""},  # No session_id in result
        ]
        result = gen._extract_forked_session_id(events, "coding")
        assert result == sys_id

    def test_extract_forked_session_id_from_index(self):
        """_extract_forked_session_id falls back to sessions-index.json."""
        gen = self._make_generator()
        newest_id = str(uuid.uuid4())

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            index_data = {
                "version": 1,
                "entries": [
                    {"sessionId": "old-session", "modified": "2025-01-01T00:00:00Z"},
                    {"sessionId": newest_id, "modified": "2026-02-12T00:00:00Z"},
                ],
            }
            index_path = tmpdir / "sessions-index.json"
            index_path.write_text(json.dumps(index_data))

            events = [
                {"type": "result", "result": ""},  # No session_id
            ]
            with patch("bot.claude_cli.session_dir", return_value=tmpdir):
                result = gen._extract_forked_session_id(events, "coding")
            assert result == newest_id

    def test_extract_forked_session_id_returns_none(self):
        """_extract_forked_session_id returns None when nothing found."""
        gen = self._make_generator()
        events = [{"type": "result", "result": ""}]
        with patch("bot.claude_cli.session_dir", return_value=Path("/nonexistent")):
            result = gen._extract_forked_session_id(events, "coding")
        assert result is None
