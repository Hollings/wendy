"""Unit tests for bot/wendy_outbox.py - tests the actual WendyOutbox implementation."""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.wendy_outbox import WendyOutbox


class TestExtractOutboxTimestamp:
    """Tests for _extract_outbox_timestamp method."""

    @pytest.fixture
    def outbox(self):
        """Create WendyOutbox with mocked bot (no polling started)."""
        with patch.object(WendyOutbox, "watch_outbox"):  # Prevent task.start()
            bot = MagicMock()
            with patch("bot.wendy_outbox.OUTBOX_DIR", Path(tempfile.mkdtemp())):
                cog = WendyOutbox(bot)
                return cog

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

        with patch.object(WendyOutbox, "watch_outbox"):  # Prevent task.start()
            with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
                with patch("bot.wendy_outbox.DB_PATH", db_path):
                    bot = MagicMock()
                    cog = WendyOutbox(bot)
                    # Store paths for test access
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

        # Mock the channel and message
        mock_channel = MagicMock()
        mock_sent_msg = MagicMock()
        mock_sent_msg.id = 111222333
        mock_sent_msg.content = "Hello from outbox"
        mock_sent_msg.attachments = []
        mock_sent_msg.channel.id = 999888777
        mock_sent_msg.guild.id = 444555666
        mock_sent_msg.author.id = 777888999
        mock_sent_msg.author.display_name = "Wendy"
        mock_sent_msg.created_at.isoformat.return_value = "2024-01-01T00:00:00"
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        # Process the file
        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        # Verify message was sent
        mock_channel.send.assert_called_once_with("Hello from outbox")

        # Verify file was deleted
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

        # File should be deleted to prevent infinite retry
        assert not outbox_file.exists()

    @pytest.mark.asyncio
    async def test_deletes_file_on_invalid_json(self, outbox_cog, temp_dirs):
        """Should delete outbox file when JSON is invalid."""
        outbox_dir, _ = temp_dirs

        outbox_file = outbox_dir / "123456_1705123456789.json"
        outbox_file.write_text("not valid json {{{")

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            await outbox_cog._process_outbox_file(outbox_file)

        # File should be deleted to prevent infinite retry
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

        # Mock the channel and message
        mock_channel = MagicMock()
        mock_sent_msg = MagicMock()
        mock_sent_msg.id = 111222333
        mock_sent_msg.content = "Test message for caching"
        mock_sent_msg.attachments = []
        mock_sent_msg.channel.id = 999888777
        mock_sent_msg.guild.id = 444555666
        mock_sent_msg.author.id = 777888999
        mock_sent_msg.author.display_name = "Wendy"
        mock_sent_msg.created_at.isoformat.return_value = "2024-01-01T00:00:00"
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        # Verify message was cached in database
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT * FROM message_history WHERE message_id = ?",
            (111222333,)
        ).fetchone()
        conn.close()

        assert row is not None
        # Row order: message_id, channel_id, guild_id, author_id, author_nickname, is_bot, content, timestamp, attachment_urls
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
        mock_sent_msg = MagicMock()
        mock_sent_msg.id = 111222333
        mock_sent_msg.content = "Using content key instead"
        mock_sent_msg.attachments = []
        mock_sent_msg.channel.id = 999888777
        mock_sent_msg.guild.id = 444555666
        mock_sent_msg.author.id = 777888999
        mock_sent_msg.author.display_name = "Wendy"
        mock_sent_msg.created_at.isoformat.return_value = "2024-01-01T00:00:00"
        mock_channel.send = AsyncMock(return_value=mock_sent_msg)

        outbox_cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("bot.wendy_outbox.OUTBOX_DIR", outbox_dir):
            with patch("bot.wendy_outbox.DB_PATH", db_path):
                await outbox_cog._process_outbox_file(outbox_file)

        mock_channel.send.assert_called_once_with("Using content key instead")


class TestTrimMessageLog:
    """Tests for _trim_message_log_if_needed method."""

    @pytest.fixture
    def outbox_with_log(self, tmp_path):
        """Create WendyOutbox with custom message log path."""
        log_file = tmp_path / "message_log.jsonl"

        with patch.object(WendyOutbox, "watch_outbox"):
            with patch("bot.wendy_outbox.OUTBOX_DIR", tmp_path / "outbox"):
                with patch("bot.wendy_outbox.MESSAGE_LOG_FILE", log_file):
                    bot = MagicMock()
                    cog = WendyOutbox(bot)
                    cog._test_log_file = log_file
                    return cog

    def test_trims_log_when_exceeds_max(self, outbox_with_log):
        """Should trim log file when it exceeds MAX_MESSAGE_LOG_LINES."""
        log_file = outbox_with_log._test_log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Write 1500 lines
        with open(log_file, "w") as f:
            for i in range(1500):
                f.write(json.dumps({"id": i}) + "\n")

        with patch("bot.wendy_outbox.MESSAGE_LOG_FILE", log_file):
            with patch("bot.wendy_outbox.MAX_MESSAGE_LOG_LINES", 1000):
                outbox_with_log._trim_message_log_if_needed()

        # Should now have exactly 1000 lines
        with open(log_file) as f:
            lines = f.readlines()
        assert len(lines) == 1000

        # Should keep the MOST RECENT lines (ids 500-1499)
        first_entry = json.loads(lines[0])
        last_entry = json.loads(lines[-1])
        assert first_entry["id"] == 500
        assert last_entry["id"] == 1499

    def test_does_not_trim_when_under_max(self, outbox_with_log):
        """Should not modify log file when under MAX_MESSAGE_LOG_LINES."""
        log_file = outbox_with_log._test_log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Write 500 lines
        with open(log_file, "w") as f:
            for i in range(500):
                f.write(json.dumps({"id": i}) + "\n")

        with patch("bot.wendy_outbox.MESSAGE_LOG_FILE", log_file):
            with patch("bot.wendy_outbox.MAX_MESSAGE_LOG_LINES", 1000):
                outbox_with_log._trim_message_log_if_needed()

        # Should still have 500 lines
        with open(log_file) as f:
            lines = f.readlines()
        assert len(lines) == 500
