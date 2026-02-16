"""Unit tests for bot/context_loader.py."""

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from bot.context_loader import (
    WENDY_USER_ID,
    build_dynamic_context,
    get_recent_messages,
    keyword_fallback,
    load_manifest,
    load_topic_files,
    select_topics,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_prompts(tmp_path, monkeypatch):
    """Set up a temporary prompts directory."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    monkeypatch.setattr("bot.context_loader.PROMPTS_DIR", prompts_dir)
    return prompts_dir


@pytest.fixture
def sample_manifest():
    """Return a sample manifest dict."""
    return {
        "always_top": ["people/deltaryz.md", "people/hollings.md"],
        "always_bottom": ["behavior.md"],
        "topics": {
            "runescape.md": {
                "description": "OSRS GE trading and bonds",
                "keywords": ["osrs", "runescape", "bond", "ge "],
            },
            "email.md": {
                "description": "Proton Mail and email",
                "keywords": ["email", "mail", "proton", "inbox"],
            },
            "twitter.md": {
                "description": "Reading tweets from Twitter/X",
                "keywords": ["tweet", "twitter", "x.com"],
            },
        },
    }


@pytest.fixture
def manifest_on_disk(tmp_prompts, sample_manifest):
    """Write a manifest to the tmp prompts dir."""
    (tmp_prompts / "manifest.json").write_text(json.dumps(sample_manifest))
    return sample_manifest


@pytest.fixture
def mock_db(tmp_path):
    """Create a temporary SQLite database with message_history table."""
    db_path = tmp_path / "wendy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE message_history (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            guild_id INTEGER,
            timestamp TEXT NOT NULL,
            author_id INTEGER,
            author_nickname TEXT,
            is_bot INTEGER DEFAULT 0,
            content TEXT,
            attachment_urls TEXT,
            reply_to_id INTEGER
        )
    """)
    conn.commit()
    return conn, db_path


# ---------------------------------------------------------------------------
# TestGetRecentMessages
# ---------------------------------------------------------------------------

class TestGetRecentMessages:
    """Tests for get_recent_messages."""

    def _insert_messages(self, conn, messages):
        for msg in messages:
            conn.execute(
                """INSERT INTO message_history
                   (message_id, channel_id, timestamp, author_id, author_nickname, content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                msg,
            )
        conn.commit()

    def test_returns_messages_oldest_first(self, mock_db):
        """Messages should be returned in oldest-first order."""
        conn, db_path = mock_db
        self._insert_messages(conn, [
            (1, 100, "2026-01-01T00:00:00", 999, "alice", "first"),
            (2, 100, "2026-01-01T00:01:00", 999, "alice", "second"),
            (3, 100, "2026-01-01T00:02:00", 999, "alice", "third"),
        ])

        result = get_recent_messages(100, count=10, db_path=db_path)

        assert len(result) == 3
        assert result[0]["content"] == "first"
        assert result[1]["content"] == "second"
        assert result[2]["content"] == "third"

    def test_filters_wendy_messages(self, mock_db):
        """Wendy's own messages should be excluded."""
        conn, db_path = mock_db
        self._insert_messages(conn, [
            (1, 100, "2026-01-01T00:00:00", 999, "alice", "hello"),
            (2, 100, "2026-01-01T00:01:00", WENDY_USER_ID, "Wendy", "hi there"),
            (3, 100, "2026-01-01T00:02:00", 999, "alice", "bye"),
        ])

        result = get_recent_messages(100, count=10, db_path=db_path)

        assert len(result) == 2
        assert all(m["author"] != "Wendy" for m in result)

    def test_filters_command_messages(self, mock_db):
        """Messages starting with ! or - should be excluded."""
        conn, db_path = mock_db
        self._insert_messages(conn, [
            (1, 100, "2026-01-01T00:00:00", 999, "alice", "!help"),
            (2, 100, "2026-01-01T00:01:00", 999, "alice", "-skip"),
            (3, 100, "2026-01-01T00:02:00", 999, "alice", "normal message"),
        ])

        result = get_recent_messages(100, count=10, db_path=db_path)

        assert len(result) == 1
        assert result[0]["content"] == "normal message"

    def test_filters_empty_content(self, mock_db):
        """Messages with empty or null content should be excluded."""
        conn, db_path = mock_db
        self._insert_messages(conn, [
            (1, 100, "2026-01-01T00:00:00", 999, "alice", ""),
            (2, 100, "2026-01-01T00:01:00", 999, "alice", None),
            (3, 100, "2026-01-01T00:02:00", 999, "alice", "real message"),
        ])

        result = get_recent_messages(100, count=10, db_path=db_path)

        assert len(result) == 1
        assert result[0]["content"] == "real message"

    def test_handles_missing_db(self, tmp_path):
        """Should return empty list if database doesn't exist."""
        result = get_recent_messages(100, db_path=tmp_path / "nonexistent.db")
        assert result == []

    def test_respects_count_limit(self, mock_db):
        """Should return at most `count` messages."""
        conn, db_path = mock_db
        self._insert_messages(conn, [
            (i, 100, f"2026-01-01T00:{i:02d}:00", 999, "alice", f"msg {i}")
            for i in range(1, 11)
        ])

        result = get_recent_messages(100, count=3, db_path=db_path)

        assert len(result) == 3


# ---------------------------------------------------------------------------
# TestLoadManifest
# ---------------------------------------------------------------------------

class TestLoadManifest:
    """Tests for load_manifest."""

    def test_valid_json(self, tmp_prompts):
        manifest = {"topics": {"a.md": {"description": "test", "keywords": []}}}
        (tmp_prompts / "manifest.json").write_text(json.dumps(manifest))
        result = load_manifest()
        assert result == manifest

    def test_missing_file(self, tmp_prompts):
        result = load_manifest()
        assert result is None

    def test_invalid_json(self, tmp_prompts):
        (tmp_prompts / "manifest.json").write_text("not valid json {{{")
        result = load_manifest()
        assert result is None


# ---------------------------------------------------------------------------
# TestLoadTopicFiles
# ---------------------------------------------------------------------------

class TestLoadTopicFiles:
    """Tests for load_topic_files."""

    def test_loads_files(self, tmp_prompts):
        (tmp_prompts / "a.md").write_text("Content A")
        (tmp_prompts / "b.md").write_text("Content B")
        result = load_topic_files(["a.md", "b.md"])
        assert "Content A" in result
        assert "Content B" in result
        assert "---" in result

    def test_skips_missing(self, tmp_prompts):
        (tmp_prompts / "a.md").write_text("Content A")
        result = load_topic_files(["a.md", "nonexistent.md"])
        assert "Content A" in result

    def test_handles_subdirectories(self, tmp_prompts):
        subdir = tmp_prompts / "people"
        subdir.mkdir()
        (subdir / "alice.md").write_text("Alice info")
        result = load_topic_files(["people/alice.md"])
        assert "Alice info" in result

    def test_empty_filenames(self, tmp_prompts):
        result = load_topic_files([])
        assert result == ""


# ---------------------------------------------------------------------------
# TestKeywordFallback
# ---------------------------------------------------------------------------

class TestKeywordFallback:
    """Tests for keyword_fallback."""

    def test_matches_keywords_case_insensitively(self, sample_manifest):
        messages = [{"author": "alice", "content": "Let's check OSRS bonds today"}]
        result = keyword_fallback(messages, sample_manifest)
        assert "runescape.md" in result

    def test_returns_empty_on_no_match(self, sample_manifest):
        messages = [{"author": "alice", "content": "hello world"}]
        result = keyword_fallback(messages, sample_manifest)
        assert result == []

    def test_handles_multiple_matches(self, sample_manifest):
        messages = [
            {"author": "alice", "content": "check my email and runescape bonds"},
        ]
        result = keyword_fallback(messages, sample_manifest)
        assert "runescape.md" in result
        assert "email.md" in result

    def test_empty_messages(self, sample_manifest):
        result = keyword_fallback([], sample_manifest)
        assert result == []

    def test_no_topics_in_manifest(self):
        manifest = {"topics": {}}
        messages = [{"author": "alice", "content": "osrs"}]
        result = keyword_fallback(messages, manifest)
        assert result == []


# ---------------------------------------------------------------------------
# TestSelectTopics
# ---------------------------------------------------------------------------

class TestSelectTopics:
    """Tests for select_topics (async)."""

    @pytest.mark.asyncio
    async def test_valid_selection(self, sample_manifest):
        """Should return valid filenames from Haiku output."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"runescape.md\nemail.md\n", b"")
        )

        with patch("bot.context_loader.asyncio.create_subprocess_exec", return_value=mock_proc):
            messages = [{"author": "alice", "content": "osrs bonds"}]
            result = await select_topics(messages, sample_manifest, "/usr/bin/claude")

        assert "runescape.md" in result
        assert "email.md" in result

    @pytest.mark.asyncio
    async def test_none_output(self, sample_manifest):
        """Should return empty list when Haiku says NONE."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"NONE", b""))

        with patch("bot.context_loader.asyncio.create_subprocess_exec", return_value=mock_proc):
            messages = [{"author": "alice", "content": "hello"}]
            result = await select_topics(messages, sample_manifest, "/usr/bin/claude")

        assert result == []

    @pytest.mark.asyncio
    async def test_timeout(self, sample_manifest):
        """Should return empty list on timeout."""
        with patch(
            "bot.context_loader.asyncio.create_subprocess_exec",
            side_effect=TimeoutError(),
        ):
            messages = [{"author": "alice", "content": "osrs"}]
            result = await select_topics(messages, sample_manifest, "/usr/bin/claude")

        assert result == []

    @pytest.mark.asyncio
    async def test_empty_messages_skip_call(self, sample_manifest):
        """Should not call CLI when messages are empty."""
        with patch("bot.context_loader.asyncio.create_subprocess_exec") as mock_exec:
            result = await select_topics([], sample_manifest, "/usr/bin/claude")

        assert result == []
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_filenames_filtered(self, sample_manifest):
        """Should filter out filenames not in manifest."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"runescape.md\nfake_topic.md\ntwitter.md", b"")
        )

        with patch("bot.context_loader.asyncio.create_subprocess_exec", return_value=mock_proc):
            messages = [{"author": "alice", "content": "stuff"}]
            result = await select_topics(messages, sample_manifest, "/usr/bin/claude")

        assert "runescape.md" in result
        assert "twitter.md" in result
        assert "fake_topic.md" not in result


# ---------------------------------------------------------------------------
# TestBuildDynamicContext
# ---------------------------------------------------------------------------

class TestBuildDynamicContext:
    """Tests for build_dynamic_context (async, integration)."""

    @pytest.mark.asyncio
    async def test_full_flow_with_haiku(self, tmp_prompts, manifest_on_disk):
        """Integration: loads manifest, gets messages, selects topics."""
        # Create topic files
        (tmp_prompts / "people").mkdir()
        (tmp_prompts / "people" / "deltaryz.md").write_text("Delta info")
        (tmp_prompts / "people" / "hollings.md").write_text("Hollings info")
        (tmp_prompts / "behavior.md").write_text("Be nice")
        (tmp_prompts / "runescape.md").write_text("OSRS stuff")

        messages = [{"author": "delta", "content": "let's check bonds"}]

        with (
            patch("bot.context_loader.get_recent_messages", return_value=messages),
            patch("bot.context_loader.select_topics", return_value=["runescape.md"]),
        ):
            result = await build_dynamic_context(100, "/usr/bin/claude")

        assert result is not None
        assert "Delta info" in result["always_top"]
        assert "Hollings info" in result["always_top"]
        assert "OSRS stuff" in result["topics"]
        assert "Be nice" in result["always_bottom"]

    @pytest.mark.asyncio
    async def test_haiku_failure_falls_back_to_keywords(self, tmp_prompts, manifest_on_disk):
        """When Haiku returns nothing, should fall back to keywords."""
        (tmp_prompts / "people").mkdir()
        (tmp_prompts / "people" / "deltaryz.md").write_text("Delta")
        (tmp_prompts / "people" / "hollings.md").write_text("Hollings")
        (tmp_prompts / "behavior.md").write_text("Behavior")
        (tmp_prompts / "runescape.md").write_text("OSRS content")

        messages = [{"author": "delta", "content": "check runescape bonds"}]

        with (
            patch("bot.context_loader.get_recent_messages", return_value=messages),
            patch("bot.context_loader.select_topics", return_value=[]),
        ):
            result = await build_dynamic_context(100, "/usr/bin/claude")

        assert result is not None
        assert "OSRS content" in result["topics"]

    @pytest.mark.asyncio
    async def test_no_manifest_returns_none(self, tmp_prompts):
        """Should return None if manifest is missing."""
        result = await build_dynamic_context(100, "/usr/bin/claude")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_messages_skips_topics(self, tmp_prompts, manifest_on_disk):
        """When no messages, topics should be empty."""
        (tmp_prompts / "people").mkdir()
        (tmp_prompts / "people" / "deltaryz.md").write_text("Delta")
        (tmp_prompts / "people" / "hollings.md").write_text("Hollings")
        (tmp_prompts / "behavior.md").write_text("Behavior")

        with patch("bot.context_loader.get_recent_messages", return_value=[]):
            result = await build_dynamic_context(100, "/usr/bin/claude")

        assert result is not None
        assert result["topics"] == ""


# ---------------------------------------------------------------------------
# TestSetupPromptsDir
# ---------------------------------------------------------------------------

class TestSetupPromptsDir:
    """Tests for setup_prompts_dir."""

    def test_seeds_files(self, tmp_prompts, tmp_path):
        """Should copy files from source to prompts dir."""
        from bot.context_loader import setup_prompts_dir

        src = tmp_path / "app_config_prompts"
        src.mkdir()
        (src / "test.md").write_text("test content")
        sub = src / "people"
        sub.mkdir()
        (sub / "alice.md").write_text("alice content")

        with patch("bot.context_loader.Path", return_value=src):
            setup_prompts_dir()

        assert (tmp_prompts / "test.md").read_text() == "test content"
        assert (tmp_prompts / "people" / "alice.md").read_text() == "alice content"

    def test_preserves_existing_files(self, tmp_prompts, tmp_path):
        """Should not overwrite files that already exist."""
        from bot.context_loader import setup_prompts_dir

        (tmp_prompts / "test.md").write_text("existing content")

        src = tmp_path / "app_config_prompts"
        src.mkdir()
        (src / "test.md").write_text("new content")

        with patch("bot.context_loader.Path", return_value=src):
            setup_prompts_dir()

        assert (tmp_prompts / "test.md").read_text() == "existing content"
