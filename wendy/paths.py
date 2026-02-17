"""Centralized path definitions for Wendy bot.

All filesystem paths used throughout the codebase are defined here.
Leaf module -- zero internal imports.
"""
from __future__ import annotations

import re
from pathlib import Path

# =============================================================================
# Base Paths
# =============================================================================

WENDY_BASE: Path = Path("/data/wendy")
CHANNELS_DIR: Path = WENDY_BASE / "channels"
SHARED_DIR: Path = WENDY_BASE / "shared"
TMP_DIR: Path = WENDY_BASE / "tmp"
FRAGMENTS_DIR: Path = WENDY_BASE / "claude_fragments"
DB_PATH: Path = SHARED_DIR / "wendy.db"
STREAM_LOG_FILE: Path = WENDY_BASE / "stream.jsonl"

# =============================================================================
# Claude Session Paths
# =============================================================================

CLAUDE_PROJECTS_DIR: Path = Path("/root/.claude/projects")


def _encode_path_for_claude(path: Path) -> str:
    """Encode a path for Claude CLI project directory naming.

    Claude CLI encodes the working directory path by replacing '/' with '-'.
    """
    return str(path).replace("/", "-")


# =============================================================================
# Channel Path Functions
# =============================================================================

CHANNEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_channel_name(name: str) -> bool:
    if not name:
        return False
    return bool(CHANNEL_NAME_PATTERN.match(name))


def channel_dir(name: str) -> Path:
    return CHANNELS_DIR / name


def beads_dir(channel_name: str) -> Path:
    return channel_dir(channel_name) / ".beads"


def session_dir(channel_name: str) -> Path:
    channel_path = channel_dir(channel_name)
    encoded = _encode_path_for_claude(channel_path)
    return CLAUDE_PROJECTS_DIR / encoded


def current_session_file(channel_name: str) -> Path:
    return channel_dir(channel_name) / ".current_session"


def claude_md_path(channel_name: str) -> Path:
    return channel_dir(channel_name) / "CLAUDE.md"


def attachments_dir(channel_name: str) -> Path:
    return channel_dir(channel_name) / "attachments"


def fragments_dir() -> Path:
    return FRAGMENTS_DIR


def journal_dir(channel_name: str) -> Path:
    return channel_dir(channel_name) / "journal"


# =============================================================================
# Utility Functions
# =============================================================================


def ensure_channel_dirs(channel_name: str, beads_enabled: bool = False) -> None:
    channel_dir(channel_name).mkdir(parents=True, exist_ok=True)
    attachments_dir(channel_name).mkdir(exist_ok=True)
    journal_dir(channel_name).mkdir(exist_ok=True)
    if beads_enabled:
        beads_dir(channel_name).mkdir(exist_ok=True)


def ensure_shared_dirs() -> None:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    FRAGMENTS_DIR.mkdir(parents=True, exist_ok=True)
