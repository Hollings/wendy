"""Unit tests for bot/fragment_loader.py."""

from unittest.mock import patch

from bot.fragment_loader import FRAGMENT_PATTERN, load_fragments, setup_fragments_dir


class TestFragmentPattern:
    """Tests for the FRAGMENT_PATTERN regex."""

    def test_common_fragment(self):
        m = FRAGMENT_PATTERN.match("common_01_personality.md")
        assert m is not None
        assert m.group(1) == "common"
        assert m.group(2) == "01"
        assert m.group(3) == "personality"

    def test_channel_id_fragment(self):
        m = FRAGMENT_PATTERN.match("1234567890123_05_coding_tools.md")
        assert m is not None
        assert m.group(1) == "1234567890123"
        assert m.group(2) == "05"
        assert m.group(3) == "coding_tools"

    def test_two_digit_order(self):
        m = FRAGMENT_PATTERN.match("common_99_last.md")
        assert m is not None
        assert m.group(2) == "99"

    def test_rejects_single_digit_order(self):
        m = FRAGMENT_PATTERN.match("common_1_bad.md")
        assert m is None

    def test_rejects_three_digit_order(self):
        m = FRAGMENT_PATTERN.match("common_100_bad.md")
        assert m is None

    def test_rejects_no_extension(self):
        m = FRAGMENT_PATTERN.match("common_01_personality")
        assert m is None

    def test_rejects_wrong_extension(self):
        m = FRAGMENT_PATTERN.match("common_01_personality.txt")
        assert m is None

    def test_rejects_random_filename(self):
        m = FRAGMENT_PATTERN.match("README.md")
        assert m is None

    def test_rejects_empty_title(self):
        m = FRAGMENT_PATTERN.match("common_01_.md")
        assert m is None


class TestLoadFragments:
    """Tests for load_fragments()."""

    def test_common_fragments_loaded(self, tmp_path):
        frag_dir = tmp_path / "claude_fragments"
        frag_dir.mkdir()
        (frag_dir / "common_01_base.md").write_text("Base instructions")
        (frag_dir / "common_10_style.md").write_text("Style guide")

        with patch("bot.fragment_loader.FRAGMENTS_DIR", frag_dir), \
             patch("bot.fragment_loader.claude_md_path", return_value=tmp_path / "nonexistent" / "CLAUDE.md"):
            result = load_fragments("9999", "testchan")

        assert "CHANNEL INSTRUCTIONS" in result
        assert "common_01_base.md" in result
        assert "common_10_style.md" in result
        assert "Base instructions" in result
        assert "Style guide" in result

    def test_channel_specific_fragments(self, tmp_path):
        frag_dir = tmp_path / "claude_fragments"
        frag_dir.mkdir()
        (frag_dir / "1234_05_tools.md").write_text("Channel tools")
        (frag_dir / "5678_05_other.md").write_text("Other channel")

        with patch("bot.fragment_loader.FRAGMENTS_DIR", frag_dir), \
             patch("bot.fragment_loader.claude_md_path", return_value=tmp_path / "nonexistent" / "CLAUDE.md"):
            result = load_fragments("1234", "testchan")

        assert "1234_05_tools.md" in result
        assert "Channel tools" in result
        assert "5678_05_other.md" not in result
        assert "Other channel" not in result

    def test_interleaved_ordering(self, tmp_path):
        """Common and channel-specific fragments should interleave by order number."""
        frag_dir = tmp_path / "claude_fragments"
        frag_dir.mkdir()
        (frag_dir / "common_01_first.md").write_text("First")
        (frag_dir / "1234_05_middle.md").write_text("Middle")
        (frag_dir / "common_10_last.md").write_text("Last")

        with patch("bot.fragment_loader.FRAGMENTS_DIR", frag_dir), \
             patch("bot.fragment_loader.claude_md_path", return_value=tmp_path / "nonexistent" / "CLAUDE.md"):
            result = load_fragments("1234", "testchan")

        first_pos = result.index("First")
        middle_pos = result.index("Middle")
        last_pos = result.index("Last")
        assert first_pos < middle_pos < last_pos

    def test_empty_fragments_skipped(self, tmp_path):
        frag_dir = tmp_path / "claude_fragments"
        frag_dir.mkdir()
        (frag_dir / "common_01_empty.md").write_text("")
        (frag_dir / "common_02_content.md").write_text("Has content")

        with patch("bot.fragment_loader.FRAGMENTS_DIR", frag_dir), \
             patch("bot.fragment_loader.claude_md_path", return_value=tmp_path / "nonexistent" / "CLAUDE.md"):
            result = load_fragments("9999", "testchan")

        assert "common_01_empty.md" not in result
        assert "common_02_content.md" in result

    def test_missing_dir_falls_back_to_legacy(self, tmp_path):
        """If fragments dir doesn't exist, fall back to legacy CLAUDE.md."""
        nonexistent = tmp_path / "nonexistent_fragments"
        legacy_path = tmp_path / "CLAUDE.md"
        legacy_path.write_text("Legacy notes here")

        with patch("bot.fragment_loader.FRAGMENTS_DIR", nonexistent), \
             patch("bot.fragment_loader.claude_md_path", return_value=legacy_path):
            result = load_fragments("9999", "testchan")

        assert "LEGACY NOTES" in result
        assert "Legacy notes here" in result

    def test_no_fragments_no_legacy_returns_empty(self, tmp_path):
        """No fragments and no legacy CLAUDE.md returns empty string."""
        frag_dir = tmp_path / "claude_fragments"
        frag_dir.mkdir()

        with patch("bot.fragment_loader.FRAGMENTS_DIR", frag_dir), \
             patch("bot.fragment_loader.claude_md_path", return_value=tmp_path / "nonexistent" / "CLAUDE.md"):
            result = load_fragments("9999", "testchan")

        assert result == ""

    def test_fragments_plus_legacy_both_included(self, tmp_path):
        """When fragments exist AND legacy CLAUDE.md exists, both are included."""
        frag_dir = tmp_path / "claude_fragments"
        frag_dir.mkdir()
        (frag_dir / "common_01_base.md").write_text("Fragment content")

        legacy_path = tmp_path / "CLAUDE.md"
        legacy_path.write_text("Legacy notes")

        with patch("bot.fragment_loader.FRAGMENTS_DIR", frag_dir), \
             patch("bot.fragment_loader.claude_md_path", return_value=legacy_path):
            result = load_fragments("9999", "testchan")

        assert "CHANNEL INSTRUCTIONS" in result
        assert "Fragment content" in result
        assert "LEGACY NOTES" in result
        assert "Legacy notes" in result

    def test_invalid_filenames_ignored(self, tmp_path):
        """Files not matching the pattern are ignored."""
        frag_dir = tmp_path / "claude_fragments"
        frag_dir.mkdir()
        (frag_dir / "README.md").write_text("Not a fragment")
        (frag_dir / "common_01_valid.md").write_text("Valid fragment")
        (frag_dir / "notes.txt").write_text("Text file")

        with patch("bot.fragment_loader.FRAGMENTS_DIR", frag_dir), \
             patch("bot.fragment_loader.claude_md_path", return_value=tmp_path / "nonexistent" / "CLAUDE.md"):
            result = load_fragments("9999", "testchan")

        assert "Valid fragment" in result
        assert "Not a fragment" not in result
        assert "Text file" not in result

    def test_subdirectories_ignored(self, tmp_path):
        """Subdirectories in the fragments dir are ignored."""
        frag_dir = tmp_path / "claude_fragments"
        frag_dir.mkdir()
        subdir = frag_dir / "common_01_subdir.md"
        subdir.mkdir()  # This is a directory, not a file
        (frag_dir / "common_02_real.md").write_text("Real fragment")

        with patch("bot.fragment_loader.FRAGMENTS_DIR", frag_dir), \
             patch("bot.fragment_loader.claude_md_path", return_value=tmp_path / "nonexistent" / "CLAUDE.md"):
            result = load_fragments("9999", "testchan")

        assert "Real fragment" in result


class TestSetupFragmentsDir:
    """Tests for setup_fragments_dir()."""

    def test_seeds_new_files(self, tmp_path):
        """Should copy files from source to dest when dest doesn't have them."""
        src_dir = tmp_path / "config" / "claude_fragments"
        src_dir.mkdir(parents=True)
        (src_dir / "common_01_base.md").write_text("Base content")

        dest_dir = tmp_path / "data" / "claude_fragments"
        dest_settings = tmp_path / "data" / "claude_fragments.json"
        src_settings = tmp_path / "config" / "claude_fragments.json"
        src_settings.write_text('{"channels": {}}')

        with patch("bot.fragment_loader.FRAGMENTS_DIR", dest_dir), \
             patch("bot.fragment_loader.FRAGMENTS_SETTINGS", dest_settings):
            # Monkey-patch the source paths used inside setup_fragments_dir
            import bot.fragment_loader as fl
            original_path = fl.Path

            def patched_path(p):
                if p == "/app/config/claude_fragments":
                    return original_path(src_dir)
                if p == "/app/config/claude_fragments.json":
                    return original_path(src_settings)
                return original_path(p)

            with patch.object(fl, "Path", side_effect=patched_path):
                setup_fragments_dir()

        assert (dest_dir / "common_01_base.md").exists()
        assert (dest_dir / "common_01_base.md").read_text() == "Base content"
        assert dest_settings.exists()

    def test_never_overwrites_existing(self, tmp_path):
        """Should not overwrite files that already exist at dest."""
        src_dir = tmp_path / "config" / "claude_fragments"
        src_dir.mkdir(parents=True)
        (src_dir / "common_01_base.md").write_text("New content")

        dest_dir = tmp_path / "data" / "claude_fragments"
        dest_dir.mkdir(parents=True)
        (dest_dir / "common_01_base.md").write_text("Existing content (Wendy's edits)")

        dest_settings = tmp_path / "data" / "claude_fragments.json"

        import bot.fragment_loader as fl
        original_path = fl.Path

        def patched_path(p):
            if p == "/app/config/claude_fragments":
                return original_path(src_dir)
            if p == "/app/config/claude_fragments.json":
                return original_path(tmp_path / "nonexistent.json")
            return original_path(p)

        with patch("bot.fragment_loader.FRAGMENTS_DIR", dest_dir), \
             patch("bot.fragment_loader.FRAGMENTS_SETTINGS", dest_settings), \
             patch.object(fl, "Path", side_effect=patched_path):
            setup_fragments_dir()

        # Should keep Wendy's edits, not overwrite
        assert (dest_dir / "common_01_base.md").read_text() == "Existing content (Wendy's edits)"
