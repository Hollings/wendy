"""Centralized path definitions for Wendy bot.

All filesystem paths used throughout the codebase are defined here.
Leaf module -- zero internal imports.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# =============================================================================
# Base Paths
# =============================================================================

WENDY_BASE: Path = Path(os.getenv("WENDY_BASE_DIR", "/data/wendy"))
CHANNELS_DIR: Path = WENDY_BASE / "channels"
SHARED_DIR: Path = WENDY_BASE / "shared"
TMP_DIR: Path = WENDY_BASE / "tmp"
FRAGMENTS_DIR: Path = WENDY_BASE / "claude_fragments"
DB_PATH: Path = SHARED_DIR / "wendy.db"
STREAM_LOG_FILE: Path = WENDY_BASE / "stream.jsonl"

# =============================================================================
# Claude Session Paths
# =============================================================================

CLAUDE_PROJECTS_DIR: Path = Path(os.getenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"


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
    dirs = [channel_dir(channel_name), attachments_dir(channel_name), journal_dir(channel_name)]
    if beads_enabled:
        dirs.append(beads_dir(channel_name))
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    # When running as root, ensure the wendy user (UID 1000) can write to channel dirs.
    # The bot process stays root; CLI subprocesses run as wendy for isolation.
    if os.name == "posix" and os.getuid() == 0:
        for d in dirs:
            try:
                os.chown(d, 1000, 1000)
            except OSError:
                pass


def find_attachments_for_message(message_id: int, channel_name: str | None = None) -> list[str]:
    """Return sorted list of attachment file paths for a given message.

    Scans the channel's attachments directory for files matching the
    ``msg_{message_id}_*`` pattern.  Returns an empty list when no
    *channel_name* is given or the directory does not exist.
    """
    if not channel_name:
        return []
    att_dir = attachments_dir(channel_name)
    if not att_dir.exists():
        return []
    return sorted(str(f) for f in att_dir.glob(f"msg_{message_id}_*"))


def ensure_shared_dirs() -> None:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    FRAGMENTS_DIR.mkdir(parents=True, exist_ok=True)
