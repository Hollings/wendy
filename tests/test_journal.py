"""Unit tests for the per-channel journal system."""

import json

from bot.claude_cli import JOURNAL_NUDGE_INTERVAL, ClaudeCliTextGenerator


class TestGetJournalSection:
    """Tests for ClaudeCliTextGenerator._get_journal_section."""

    def _make_generator(self, monkeypatch):
        """Create a ClaudeCliTextGenerator with mocked CLI path."""
        monkeypatch.setattr(
            ClaudeCliTextGenerator, "_find_cli_path", lambda self: "/usr/bin/claude"
        )
        monkeypatch.setattr(
            ClaudeCliTextGenerator, "_migrate_legacy_session_state", lambda self: None
        )
        return ClaudeCliTextGenerator(model="sonnet")

    def test_empty_journal_shows_no_entries_message(self, tmp_path, monkeypatch):
        """When journal dir is empty, should show 'No entries yet' message."""
        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: tmp_path / "journal")
        gen = self._make_generator(monkeypatch)

        section = gen._get_journal_section("test-channel")

        assert "JOURNAL (your long-term memory)" in section
        assert "(No entries yet - start writing!)" in section
        assert "JOURNAL REMINDER" not in section

    def test_file_tree_listing(self, tmp_path, monkeypatch):
        """Journal files should appear in the file tree listing."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()
        (j_dir / "2026-01-28_first-day.md").write_text("hello")
        (j_dir / "2026-02-01_cooking.md").write_text("world")

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        section = gen._get_journal_section("test-channel")

        assert "2026-01-28_first-day.md" in section
        assert "2026-02-01_cooking.md" in section
        assert "(No entries yet" not in section

    def test_dotfiles_excluded_from_listing(self, tmp_path, monkeypatch):
        """Dotfiles like .nudge_state should not appear in the file tree."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()
        (j_dir / ".nudge_state").write_text("{}")
        (j_dir / ".hidden").write_text("secret")
        (j_dir / "2026-02-05_visible.md").write_text("hi")

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        section = gen._get_journal_section("test-channel")

        assert ".nudge_state" not in section
        assert ".hidden" not in section
        assert "2026-02-05_visible.md" in section

    def test_nudge_fires_after_n_invocations(self, tmp_path, monkeypatch):
        """After JOURNAL_NUDGE_INTERVAL invocations with no writes, nudge should appear."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        # Call N times (the interval)
        for _ in range(JOURNAL_NUDGE_INTERVAL):
            section = gen._get_journal_section("test-channel")

        # The Nth call should have the nudge
        assert "JOURNAL REMINDER" in section

    def test_no_nudge_before_interval(self, tmp_path, monkeypatch):
        """Before reaching the interval, no nudge should appear."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        # Call fewer than N times
        for _ in range(JOURNAL_NUDGE_INTERVAL - 1):
            section = gen._get_journal_section("test-channel")

        assert "JOURNAL REMINDER" not in section

    def test_counter_resets_on_journal_write(self, tmp_path, monkeypatch):
        """Writing a new journal file should reset the invocation counter."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        # Build up invocations close to the threshold
        for _ in range(JOURNAL_NUDGE_INTERVAL - 2):
            gen._get_journal_section("test-channel")

        # Simulate Wendy writing a journal entry (new file appears)
        (j_dir / "2026-02-05_new-entry.md").write_text("I learned something today")

        # Next call should detect the new file and reset counter (count becomes 1)
        section = gen._get_journal_section("test-channel")
        assert "JOURNAL REMINDER" not in section

        # After reset, need JOURNAL_NUDGE_INTERVAL - 2 more calls to stay below threshold
        # (reset call already set count to 1)
        for _ in range(JOURNAL_NUDGE_INTERVAL - 2):
            section = gen._get_journal_section("test-channel")
        assert "JOURNAL REMINDER" not in section

        # The next call should hit the threshold and trigger the nudge
        section = gen._get_journal_section("test-channel")
        assert "JOURNAL REMINDER" in section

    def test_preexisting_files_dont_reset_counter(self, tmp_path, monkeypatch):
        """Files that exist before tracking starts should not reset the counter."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()
        (j_dir / "2026-01-01_old-entry.md").write_text("old stuff")

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        # Even though a file exists, the counter should still count up normally
        for _ in range(JOURNAL_NUDGE_INTERVAL):
            section = gen._get_journal_section("test-channel")

        assert "JOURNAL REMINDER" in section

    def test_missing_nudge_state_handled_gracefully(self, tmp_path, monkeypatch):
        """Missing .nudge_state file should not raise an error."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        # Should work without any .nudge_state file
        section = gen._get_journal_section("test-channel")
        assert "JOURNAL" in section

    def test_corrupt_nudge_state_handled_gracefully(self, tmp_path, monkeypatch):
        """Corrupt .nudge_state JSON should not raise an error."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()
        (j_dir / ".nudge_state").write_text("not valid json{{{")

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        section = gen._get_journal_section("test-channel")
        assert "JOURNAL" in section

    def test_journal_dir_created_if_missing(self, tmp_path, monkeypatch):
        """Journal dir should be created if it doesn't exist yet."""
        j_dir = tmp_path / "nonexistent" / "journal"
        assert not j_dir.exists()

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        gen._get_journal_section("test-channel")
        assert j_dir.exists()

    def test_nudge_state_persists_across_calls(self, tmp_path, monkeypatch):
        """Invocation count should persist in .nudge_state between calls."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        # Call 3 times
        for _ in range(3):
            gen._get_journal_section("test-channel")

        # Check the state file
        state = json.loads((j_dir / ".nudge_state").read_text())
        assert state["invocations_since_write"] == 3

    def test_section_includes_journal_path(self, tmp_path, monkeypatch):
        """The section should include the full path to the journal dir."""
        j_dir = tmp_path / "journal"
        j_dir.mkdir()

        monkeypatch.setattr("bot.claude_cli.journal_dir", lambda name: j_dir)
        gen = self._make_generator(monkeypatch)

        section = gen._get_journal_section("test-channel")
        assert str(j_dir) in section
