"""Unit tests for the per-channel journal system.

Ported from bot.claude_cli tests.  The old ClaudeCliTextGenerator class
and nudge-interval tracking were removed in the v2 rewrite.  Journal
functionality is now split between:
  - wendy.prompt._get_journal_section  (static system prompt section)
  - wendy.prompt.get_journal_listing_for_nudge  (compact file listing)
"""

from unittest.mock import patch

from wendy.prompt import _get_journal_section, get_journal_listing_for_nudge


class TestGetJournalSection:
    """Tests for _get_journal_section."""

    def test_section_includes_journal_header(self, tmp_path):
        """Should always include the JOURNAL header."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        with patch("wendy.prompt.journal_dir", return_value=j_dir):
            section = _get_journal_section("test-channel")

        assert "JOURNAL" in section

    def test_section_includes_journal_path(self, tmp_path):
        """The section should include the full path to the journal dir."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        with patch("wendy.prompt.journal_dir", return_value=j_dir):
            section = _get_journal_section("test-channel")

        assert str(j_dir) in section

    def test_journal_dir_created_if_missing(self, tmp_path):
        """Journal dir should be created if it doesn't exist yet."""
        j_dir = tmp_path / "nonexistent" / "journal"
        assert not j_dir.exists()

        with patch("wendy.prompt.journal_dir", return_value=j_dir):
            _get_journal_section("test-channel")

        assert j_dir.exists()


class TestGetJournalListingForNudge:
    """Tests for get_journal_listing_for_nudge."""

    def test_file_listing(self, tmp_path):
        """Journal files should appear in the listing."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()
        (j_dir / "2026-01-28_first-day.md").write_text("hello")
        (j_dir / "2026-02-01_cooking.md").write_text("world")

        with patch("wendy.prompt.journal_dir", return_value=j_dir):
            listing = get_journal_listing_for_nudge("test-channel")

        assert "2026-01-28_first-day.md" in listing
        assert "2026-02-01_cooking.md" in listing
        assert "2 files" in listing

    def test_empty_dir_returns_empty_string(self, tmp_path):
        """Empty journal dir should return empty string."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        with patch("wendy.prompt.journal_dir", return_value=j_dir):
            listing = get_journal_listing_for_nudge("test-channel")

        assert listing == ""

    def test_dotfiles_excluded_from_listing(self, tmp_path):
        """Dotfiles like .nudge_state should not appear in the listing."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()
        (j_dir / ".nudge_state").write_text("{}")
        (j_dir / ".hidden").write_text("secret")
        (j_dir / "2026-02-05_visible.md").write_text("hi")

        with patch("wendy.prompt.journal_dir", return_value=j_dir):
            listing = get_journal_listing_for_nudge("test-channel")

        assert ".nudge_state" not in listing
        assert ".hidden" not in listing
        assert "2026-02-05_visible.md" in listing

    def test_missing_dir_returns_empty_string(self, tmp_path):
        """Missing journal dir should return empty string."""
        j_dir = tmp_path / "nonexistent" / "journal"

        with patch("wendy.prompt.journal_dir", return_value=j_dir):
            listing = get_journal_listing_for_nudge("test-channel")

        assert listing == ""
