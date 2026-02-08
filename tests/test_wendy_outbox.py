"""Unit tests for bot/wendy_outbox.py - tests the actual WendyOutbox implementation."""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.wendy_outbox import WendyOutbox


def _make_outbox_cog(outbox_dir, db_path=None):
    """Helper to create a WendyOutbox with mocked tasks and temp directories."""
    patches = [
        patch.object(WendyOutbox, "watch_outbox"),
        patch.object(WendyOutbox, "refresh_emoji_cache"),
        patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir),
    ]
    if db_path:
        patches.append(patch("bot.wendy_outbox.DB_PATH", db_path))

    for p in patches:
        p.start()

    bot = MagicMock()
    cog = WendyOutbox(bot)

    for p in patches:
        p.stop()

    return cog


def _mock_sent_message(msg_id=111222333, content="", channel_id=999888777):
    """Create a mock discord.Message for send() return values."""
    mock = MagicMock()
    mock.id = msg_id
    mock.content = content
    mock.attachments = []
    mock.channel.id = channel_id
    mock.guild.id = 444555666
    mock.author.id = 777888999
    mock.author.display_name = "Wendy"
    mock.created_at.isoformat.return_value = "2024-01-01T00:00:00"
    return mock


class TestExtractOutboxTimestamp:
    """Tests for _extract_outbox_timestamp method."""

    @pytest.fixture
    def outbox(self):
        """Create WendyOutbox with mocked bot (no polling started)."""
        return _make_outbox_cog(Path(tempfile.mkdtemp()))

    def test_valid_filename(self, outbox):
        """Should extract timestamp from valid filename."""
        result = outbox._extract_outbox_timestamp("123456_1705123456789.json")
        assert result == 1705123456789

    def test_different_channel_id_lengths(self, outbox):
        """Should work with different channel ID lengths."""
        result = outbox._extract_outbox_timestamp("999999999999999999_1705123456789.json")
        assert result == 1705123456789

    def test_invalid_extension_returns_none(self, outbox):
        """Should return None for non-json files."""
        assert outbox._extract_outbox_timestamp("123456_1705123456789.txt") is None

    def test_missing_underscore_returns_none(self, outbox):
        """Should return None for filenames without underscore."""
        assert outbox._extract_outbox_timestamp("1705123456789.json") is None

    def test_non_numeric_parts_returns_none(self, outbox):
        """Should return None for non-numeric parts."""
        assert outbox._extract_outbox_timestamp("abc_def.json") is None

    def test_empty_string_returns_none(self, outbox):
        """Should return None for empty string."""
        assert outbox._extract_outbox_timestamp("") is None


class TestProcessOutboxFile:
    """Tests for _process_outbox_file async method."""

    @pytest.fixture
    def temp_dirs(self):
        """Create temp directories for outbox and database."""
        outbox_dir = Path(tempfile.mkdtemp())
        db_dir = Path(tempfile.mkdtemp())
        db_path = db_dir / "wendy.db"
        return outbox_dir, db_path

    @pytest.fixture
    def outbox_cog(self, temp_dirs):
        """Create WendyOutbox with mocked bot and temp directories."""
        outbox_dir, db_path = temp_dirs

        # Initialize the database with required schema
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_history (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                guild_id INTEGER,
                author_id INTEGER,
                author_nickname TEXT,
                is_bot INTEGER,
                content TEXT,
                timestamp TEXT,
                attachment_urls TEXT
            )
        """)
        conn.commit()
        conn.close()

        cog = _make_outbox_cog(outbox_dir, db_path)
        cog._test_outbox_dir = outbox_dir
        cog._test_db_path = db_path
        return cog

    @pytest.mark.asyncio
    async def test_sends_message_to_correct_channel(self, outbox_cog, temp_dirs):
        """Should send message content to the channel specified in the JSON file."""
        outbox_dir, db_path = temp_dirs

        # Create outbox file
        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "message": "Hello from outbox"
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="Hello from outbox")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        mock_channel.send.assert_called_once_with("Hello from outbox")
        assert not outbox_file.exists()

    @pytest.mark.asyncio
    async def test_deletes_file_when_channel_not_found(self, outbox_cog, temp_dirs):
        """Should delete outbox file even when channel is not found."""
        outbox_dir, _ = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "message": "Hello"
        }))

        outbox_cog.bot.get_channel = MagicMock(return_value=None)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            await outbox_cog._process_outbox_file(outbox_file)

        assert not outbox_file.exists()

    @pytest.mark.asyncio
    async def test_deletes_file_on_invalid_json(self, outbox_cog, temp_dirs):
        """Should delete outbox file when JSON is invalid."""
        outbox_dir, _ = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text("not valid json {{{")

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            await outbox_cog._process_outbox_file(outbox_file)

        assert not outbox_file.exists()

    @pytest.mark.asyncio
    async def test_caches_sent_message_to_database(self, outbox_cog, temp_dirs):
        """Should cache Wendy's sent message to the message_history table."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "message": "Test message for caching"
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="Test message for caching")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT * FROM message_history WHERE message_id = ?",
            (111222333,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == 111222333  # message_id
        assert row[1] == 999888777  # channel_id
        assert row[5] == 1  # is_bot should be True

    @pytest.mark.asyncio
    async def test_accepts_content_key_as_alias_for_message(self, outbox_cog, temp_dirs):
        """Should accept 'content' key as an alias for 'message'."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "content": "Using content key instead"
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="Using content key instead")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        mock_channel.send.assert_called_once_with("Using content key instead")


class TestProcessSingleMessageWithReply:
    """Tests for reply_to support in _process_single_message."""

    @pytest.fixture
    def temp_dirs(self):
        outbox_dir = Path(tempfile.mkdtemp())
        db_dir = Path(tempfile.mkdtemp())
        db_path = db_dir / "wendy.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_history (
                message_id INTEGER PRIMARY KEY, channel_id INTEGER,
                guild_id INTEGER, author_id INTEGER, author_nickname TEXT,
                is_bot INTEGER, content TEXT, timestamp TEXT, attachment_urls TEXT
            )
        """)
        conn.commit()
        conn.close()
        return outbox_dir, db_path

    @pytest.fixture
    def outbox_cog(self, temp_dirs):
        outbox_dir, db_path = temp_dirs
        cog = _make_outbox_cog(outbox_dir, db_path)
        cog._test_outbox_dir = outbox_dir
        cog._test_db_path = db_path
        return cog

    @pytest.mark.asyncio
    async def test_reply_to_creates_message_reference(self, outbox_cog, temp_dirs):
        """Should create a MessageReference when reply_to is provided."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "message": "replying to you",
            "reply_to": 5555555555
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="replying to you")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        # Verify send was called with reference and mention_author=False
        call_args = mock_channel.send.call_args
        assert call_args[0][0] == "replying to you"
        ref = call_args[1]["reference"]
        assert ref.message_id == 5555555555
        assert ref.channel_id == 999888777
        assert call_args[1]["mention_author"] is False

    @pytest.mark.asyncio
    async def test_no_reply_to_sends_without_reference(self, outbox_cog, temp_dirs):
        """Should send without reference when reply_to is absent."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "message": "no reply"
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="no reply")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        # Should be called without reference kwargs
        mock_channel.send.assert_called_once_with("no reply")

    @pytest.mark.asyncio
    async def test_invalid_reply_to_sends_without_reference(self, outbox_cog, temp_dirs):
        """Should gracefully handle invalid reply_to and send without reference."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "message": "bad reply",
            "reply_to": "not-a-number"
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="bad reply")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        # Should still send the message (just without reference)
        mock_channel.send.assert_called_once_with("bad reply")


class TestProcessBatchActions:
    """Tests for batch action processing in _process_actions."""

    @pytest.fixture
    def temp_dirs(self):
        outbox_dir = Path(tempfile.mkdtemp())
        db_dir = Path(tempfile.mkdtemp())
        db_path = db_dir / "wendy.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_history (
                message_id INTEGER PRIMARY KEY, channel_id INTEGER,
                guild_id INTEGER, author_id INTEGER, author_nickname TEXT,
                is_bot INTEGER, content TEXT, timestamp TEXT, attachment_urls TEXT
            )
        """)
        conn.commit()
        conn.close()
        return outbox_dir, db_path

    @pytest.fixture
    def outbox_cog(self, temp_dirs):
        outbox_dir, db_path = temp_dirs
        cog = _make_outbox_cog(outbox_dir, db_path)
        cog._test_outbox_dir = outbox_dir
        cog._test_db_path = db_path
        return cog

    @pytest.mark.asyncio
    async def test_send_message_action(self, outbox_cog, temp_dirs):
        """Should send a message from a send_message action."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "actions": [
                {"type": "send_message", "content": "hello from batch"}
            ]
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="hello from batch")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        mock_channel.send.assert_called_once_with("hello from batch")
        assert not outbox_file.exists()

    @pytest.mark.asyncio
    async def test_add_reaction_action(self, outbox_cog, temp_dirs):
        """Should add a reaction from an add_reaction action."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "actions": [
                {"type": "add_reaction", "message_id": 5555555555, "emoji": "thumbsup"}
            ]
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_target_msg = MagicMock()
        mock_target_msg.add_reaction = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_target_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        mock_channel.fetch_message.assert_called_once_with(5555555555)
        mock_target_msg.add_reaction.assert_called_once_with("thumbsup")
        assert not outbox_file.exists()

    @pytest.mark.asyncio
    async def test_multiple_actions_in_order(self, outbox_cog, temp_dirs):
        """Should process multiple actions in order."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "actions": [
                {"type": "send_message", "content": "nice work!", "reply_to": 5555555555},
                {"type": "add_reaction", "message_id": 5555555555, "emoji": "fire"}
            ]
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="nice work!")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        mock_target_msg = MagicMock()
        mock_target_msg.add_reaction = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_target_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        # Both actions should have been processed
        assert mock_channel.send.call_count == 1
        assert mock_target_msg.add_reaction.call_count == 1
        mock_target_msg.add_reaction.assert_called_with("fire")

        # Send should have reply reference
        call_args = mock_channel.send.call_args
        assert call_args[1]["reference"].message_id == 5555555555

    @pytest.mark.asyncio
    async def test_error_in_one_action_does_not_block_others(self, outbox_cog, temp_dirs):
        """Error in first action should not prevent second action from running."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "actions": [
                {"type": "add_reaction", "message_id": 9999, "emoji": "fire"},
                {"type": "send_message", "content": "still works"}
            ]
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        # First action fails: fetch_message raises
        mock_channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
        # Second action succeeds
        mock_sent_msg = _mock_sent_message(content="still works")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        # Second action (send_message) should still succeed
        mock_channel.send.assert_called_once_with("still works")
        assert not outbox_file.exists()

    @pytest.mark.asyncio
    async def test_unknown_action_type_logged_and_skipped(self, outbox_cog, temp_dirs):
        """Unknown action types should be logged and skipped without error."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "actions": [
                {"type": "do_a_dance"},
                {"type": "send_message", "content": "after unknown"}
            ]
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="after unknown")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        # send_message should still work after unknown action
        mock_channel.send.assert_called_once_with("after unknown")

    @pytest.mark.asyncio
    async def test_legacy_format_still_works(self, outbox_cog, temp_dirs):
        """Legacy format (no actions key) should still work as before."""
        outbox_dir, db_path = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text(json.dumps({
            "channel_id": "999888777",
            "message": "legacy format"
        }))

        mock_channel = MagicMock()
        mock_channel.id = 999888777
        mock_sent_msg = _mock_sent_message(content="legacy format")
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        mock_channel.send.assert_called_once_with("legacy format")


class TestBuildAttachment:
    """Tests for _build_attachment helper method."""

    @pytest.fixture
    def outbox_cog(self):
        return _make_outbox_cog(Path(tempfile.mkdtemp()))

    def test_none_path_returns_none(self, outbox_cog):
        """Should return None when file_path_str is None."""
        result = outbox_cog._build_attachment(None, MagicMock())
        assert result is None

    def test_missing_file_returns_none(self, outbox_cog):
        """Should return None when file doesn't exist."""
        result = outbox_cog._build_attachment("/nonexistent/file.png", MagicMock())
        assert result is None

    def test_valid_file_returns_discord_file(self, outbox_cog, tmp_path):
        """Should return a discord.File for a valid file."""
        test_file = tmp_path / "test.png"
        test_file.write_bytes(b"fake image data")

        result = outbox_cog._build_attachment(str(test_file), MagicMock())
        assert isinstance(result, discord.File)

    def test_oversized_file_returns_none(self, outbox_cog, tmp_path):
        """Should return None when file exceeds MAX_FILE_SIZE_MB."""
        test_file = tmp_path / "big.bin"
        # Create a file just over the limit (write a small file but mock the stat)
        test_file.write_bytes(b"x")

        with patch("bot.wendy_outbox.MAX_FILE_SIZE_MB", 0):
            result = outbox_cog._build_attachment(str(test_file), MagicMock())
        assert result is None


class TestEmojiCache:
    """Tests for the refresh_emoji_cache task."""

    @pytest.fixture
    def outbox_cog(self, tmp_path):
        cog = _make_outbox_cog(Path(tempfile.mkdtemp()))
        return cog

    @pytest.mark.asyncio
    async def test_writes_cache_file(self, outbox_cog, tmp_path):
        """Should write emoji cache file with correct structure."""
        cache_file = tmp_path / "emojis.json"

        mock_emoji = MagicMock()
        mock_emoji.name = "pepe"
        mock_emoji.id = 123456789
        mock_emoji.animated = False

        mock_guild = MagicMock()
        mock_guild.name = "Test Server"
        mock_guild.emojis = [mock_emoji]

        outbox_cog.bot.guilds = [mock_guild]

        with patch("bot.wendy_outbox.EMOJI_CACHE_FILE", cache_file):
            await outbox_cog.refresh_emoji_cache()

        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert len(data) == 1
        assert data[0]["name"] == "pepe"
        assert data[0]["id"] == "123456789"
        assert data[0]["animated"] is False
        assert data[0]["guild"] == "Test Server"
        assert data[0]["usage"] == "<:pepe:123456789>"

    @pytest.mark.asyncio
    async def test_animated_emoji_format(self, outbox_cog, tmp_path):
        """Should use 'a' prefix for animated emojis."""
        cache_file = tmp_path / "emojis.json"

        mock_emoji = MagicMock()
        mock_emoji.name = "dance"
        mock_emoji.id = 987654321
        mock_emoji.animated = True

        mock_guild = MagicMock()
        mock_guild.name = "Test Server"
        mock_guild.emojis = [mock_emoji]

        outbox_cog.bot.guilds = [mock_guild]

        with patch("bot.wendy_outbox.EMOJI_CACHE_FILE", cache_file):
            await outbox_cog.refresh_emoji_cache()

        data = json.loads(cache_file.read_text())
        assert data[0]["usage"] == "<a:dance:987654321>"

    @pytest.mark.asyncio
    async def test_handles_no_guilds(self, outbox_cog, tmp_path):
        """Should write empty array when bot has no guilds."""
        cache_file = tmp_path / "emojis.json"
        outbox_cog.bot.guilds = []

        with patch("bot.wendy_outbox.EMOJI_CACHE_FILE", cache_file):
            await outbox_cog.refresh_emoji_cache()

        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert data == []


class TestTrimMessageLog:
    """Tests for _trim_message_log_if_needed method."""

    @pytest.fixture
    def outbox_with_log(self, tmp_path):
        """Create WendyOutbox with custom message log path."""
        log_file = tmp_path / "message_log.jsonl"
        cog = _make_outbox_cog(tmp_path / "outbox")
        cog._test_log_file = log_file
        return cog

    def test_trims_log_when_exceeds_max(self, outbox_with_log):
        """Should trim log file when it exceeds MAX_MESSAGE_LOG_LINES."""
        log_file = outbox_with_log._test_log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)

        with open(log_file, "w") as f:
            for i in range(1500):
                f.write(json.dumps({"id": i}) + "\n")

        with patch("bot.wendy_outbox.MESSAGE_LOG_FILE", log_file):
            with patch("bot.wendy_outbox.MAX_MESSAGE_LOG_LINES", 1000):
                outbox_with_log._trim_message_log_if_needed()

        with open(log_file) as f:
            lines = f.readlines()
        assert len(lines) == 1000

        first_entry = json.loads(lines[0])
        last_entry = json.loads(lines[-1])
        assert first_entry["id"] == 500
        assert last_entry["id"] == 1499

    def test_does_not_trim_when_under_max(self, outbox_with_log):
        """Should not modify log file when under MAX_MESSAGE_LOG_LINES."""
        log_file = outbox_with_log._test_log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)

        with open(log_file, "w") as f:
            for i in range(500):
                f.write(json.dumps({"id": i}) + "\n")

        with patch("bot.wendy_outbox.MESSAGE_LOG_FILE", log_file):
            with patch("bot.wendy_outbox.MAX_MESSAGE_LOG_LINES", 1000):
                outbox_with_log._trim_message_log_if_needed()

        with open(log_file) as f:
            lines = f.readlines()
        assert len(lines) == 500
