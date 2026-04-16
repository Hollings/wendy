"""Unit tests for wendy.fragment_setup.setup_fragments_dir.

Ported from bot.fragment_loader tests.  The old FRAGMENT_PATTERN regex
and the v1 load_fragments function were removed in the v2 rewrite
(load_fragments now uses YAML frontmatter and is tested in test_fragments.py).
Only setup_fragments_dir survives, now in wendy.fragment_setup.
"""

from unittest.mock import patch

from wendy.fragment_setup import setup_fragments_dir


class TestSetupFragmentsDir:
    """Tests for setup_fragments_dir()."""

    def test_seeds_new_files(self, tmp_path):
        """Should copy files from source to dest when dest doesn't have them."""
        src_dir = tmp_path / "config" / "claude_fragments"
        src_dir.mkdir(parents=True)
        (src_dir / "common_personality.md").write_text(
            "---\ntype: common\norder: 1\n---\nBase content"
        )

        dest_dir = tmp_path / "data" / "claude_fragments"

        import wendy.fragment_setup as fs
        original_path = fs.Path

        def patched_path(p):
            if p == "/app/config/claude_fragments":
                return original_path(src_dir)
            return original_path(p)

        with patch("wendy.fragment_setup.FRAGMENTS_DIR", dest_dir), \
             patch.object(fs, "Path", side_effect=patched_path):
            setup_fragments_dir()

        assert (dest_dir / "common_personality.md").exists()
        assert "Base content" in (dest_dir / "common_personality.md").read_text()

    def test_seeds_subdirectories(self, tmp_path):
        """Should copy files in subdirectories (e.g. people/)."""
        src_dir = tmp_path / "config" / "claude_fragments"
        people_dir = src_dir / "people"
        people_dir.mkdir(parents=True)
        (people_dir / "alice.md").write_text("Alice info")

        dest_dir = tmp_path / "data" / "claude_fragments"

        import wendy.fragment_setup as fs
        original_path = fs.Path

        def patched_path(p):
            if p == "/app/config/claude_fragments":
                return original_path(src_dir)
            return original_path(p)

        with patch("wendy.fragment_setup.FRAGMENTS_DIR", dest_dir), \
             patch.object(fs, "Path", side_effect=patched_path):
            setup_fragments_dir()

        assert (dest_dir / "people" / "alice.md").exists()
        assert (dest_dir / "people" / "alice.md").read_text() == "Alice info"

    def test_never_overwrites_existing(self, tmp_path):
        """Should not overwrite files that already exist at dest."""
        src_dir = tmp_path / "config" / "claude_fragments"
        src_dir.mkdir(parents=True)
        (src_dir / "common_personality.md").write_text("New content from repo")

        dest_dir = tmp_path / "data" / "claude_fragments"
        dest_dir.mkdir(parents=True)
        (dest_dir / "common_personality.md").write_text(
            "Existing content (Wendy's edits)"
        )

        import wendy.fragment_setup as fs
        original_path = fs.Path

        def patched_path(p):
            if p == "/app/config/claude_fragments":
                return original_path(src_dir)
            return original_path(p)

        with patch("wendy.fragment_setup.FRAGMENTS_DIR", dest_dir), \
             patch.object(fs, "Path", side_effect=patched_path):
            setup_fragments_dir()

        # Should keep Wendy's edits, not overwrite
        assert (dest_dir / "common_personality.md").read_text() == (
            "Existing content (Wendy's edits)"
        )

    def test_missing_source_dir_is_noop(self, tmp_path):
        """Should do nothing when the source config dir doesn't exist."""
        dest_dir = tmp_path / "data" / "claude_fragments"

        import wendy.fragment_setup as fs
        original_path = fs.Path

        def patched_path(p):
            if p == "/app/config/claude_fragments":
                return original_path(tmp_path / "nonexistent")
            return original_path(p)

        with patch("wendy.fragment_setup.FRAGMENTS_DIR", dest_dir), \
             patch.object(fs, "Path", side_effect=patched_path):
            setup_fragments_dir()

        assert not dest_dir.exists()
