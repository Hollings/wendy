"""Unit tests for the per-channel journal system.

Ported from bot.claude_cli tests.  The old ClaudeCliTextGenerator class
and nudge-interval tracking were removed in the v2 rewrite.  Journal
functionality lives in wendy.prompt._get_journal_section (static system
prompt section). The compact file listing previously injected into every
nudge has been moved to the journal_stop_check.sh hook.
"""

from unittest.mock import patch

from wendy.prompt import _get_journal_section


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


