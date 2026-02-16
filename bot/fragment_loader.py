"""Fragment-based CLAUDE.md loader for Wendy bot.

Replaces the monolithic per-channel CLAUDE.md with a folder of named fragments
that are scanned, sorted by order, and assembled into a single prompt section.

Fragment filenames follow the convention:
    {identifier}_{order}_{descriptive_title}.md

Where identifier is either "common" (loaded for all channels) or a numeric
Discord channel ID, order is a zero-padded 2-digit number (01-99), and
descriptive_title is an underscore-separated description.

Examples:
    common_01_personality.md
    common_10_code_style.md
    1234567890123_05_coding_tools.md
    1234567890123_50_self_notes.md
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from .paths import FRAGMENTS_DIR, FRAGMENTS_SETTINGS, claude_md_path

_LOG = logging.getLogger(__name__)

FRAGMENT_PATTERN = re.compile(r"^(common|\d+)_(\d{2})_(.+)\.md$")


def load_fragments(channel_id: str, channel_name: str) -> str:
    """Load and assemble fragments for a channel.

    Scans the fragments directory for files matching common_*.md and
    {channel_id}_*.md, sorts by order number, and concatenates with
    separators. Falls back to legacy CLAUDE.md if no fragments exist.

    Args:
        channel_id: Discord channel ID (as string).
        channel_name: Channel folder name (for legacy CLAUDE.md fallback).

    Returns:
        Assembled prompt section string, or empty string if nothing to load.
    """
    frag_dir = FRAGMENTS_DIR
    if not frag_dir.exists():
        return _legacy_notes(channel_name)

    matches = []
    for f in frag_dir.iterdir():
        if not f.is_file():
            continue
        m = FRAGMENT_PATTERN.match(f.name)
        if not m:
            continue
        identifier, order, _ = m.group(1), int(m.group(2)), m.group(3)
        if identifier == "common" or identifier == str(channel_id):
            matches.append((order, f.name, f))

    if not matches and not _has_legacy_notes(channel_name):
        return ""

    matches.sort(key=lambda x: x[0])

    sections = []
    for _order, name, path in matches:
        content = path.read_text().strip()
        if content:
            sections.append(f"--- {name} ---\n{content}")

    result = ""
    if sections:
        result = (
            "\n\n---\n"
            "CHANNEL INSTRUCTIONS (from /data/wendy/claude_fragments/ - you can edit these files):\n"
        )
        result += "\n".join(sections)
        result += "\n---"

    # Backward compat: append old CLAUDE.md if it has content
    legacy = _legacy_notes(channel_name)
    if legacy:
        result += legacy

    return result


def _has_legacy_notes(channel_name: str) -> bool:
    """Check if a legacy CLAUDE.md file exists with content."""
    notes_path = claude_md_path(channel_name)
    if not notes_path.exists():
        return False
    try:
        return bool(notes_path.read_text().strip())
    except Exception:
        return False


def _legacy_notes(channel_name: str) -> str:
    """Load legacy per-channel CLAUDE.md as a deprecated fallback section."""
    notes_path = claude_md_path(channel_name)
    if not notes_path.exists():
        return ""
    try:
        content = notes_path.read_text().strip()
        if content:
            return (
                f"\n\n--- LEGACY NOTES (from channels/{channel_name}/CLAUDE.md"
                f" - migrate to a fragment file) ---\n{content}\n---"
            )
        return ""
    except Exception as e:
        _LOG.warning("Failed to read legacy notes: %s", e)
        return ""


def setup_fragments_dir() -> None:
    """Seed fragment files from /app/config/claude_fragments/ to FRAGMENTS_DIR.

    Also seeds the settings file from /app/config/claude_fragments.json.
    Only copies files that don't already exist, preserving runtime edits.
    """
    # Seed fragment files
    src_dir = Path("/app/config/claude_fragments")
    if src_dir.exists():
        FRAGMENTS_DIR.mkdir(parents=True, exist_ok=True)

        for src_file in src_dir.rglob("*"):
            if not src_file.is_file():
                continue

            rel_path = src_file.relative_to(src_dir)
            dest_file = FRAGMENTS_DIR / rel_path

            if dest_file.exists():
                continue

            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            _LOG.info("Seeded fragment file: %s", rel_path)
    else:
        _LOG.info("No source fragments dir at %s, skipping seed", src_dir)

    # Seed settings file
    src_settings = Path("/app/config/claude_fragments.json")
    if src_settings.exists() and not FRAGMENTS_SETTINGS.exists():
        shutil.copy2(src_settings, FRAGMENTS_SETTINGS)
        _LOG.info("Seeded fragments settings file")
