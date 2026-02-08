"""Centralized path definitions for Wendy bot.

This module provides a single source of truth for all filesystem paths used
throughout the Wendy bot codebase. All paths are derived from WENDY_BASE to
ensure consistency and make it easy to relocate the data directory.

Directory Structure:
    /data/wendy/
    +-- channels/              # Per-channel workspaces
    |   +-- coding/            # Channel with beads_enabled: true
    |   |   +-- CLAUDE.md      # Wendy's personal notes
    |   |   +-- attachments/   # Downloaded Discord files (per-channel)
    |   |   +-- .claude/       # Claude settings
    |   |   +-- .beads/        # Task queue (only if beads_enabled)
    |   |   +-- .current_session  # Session ID for agent forking
    |   +-- chat/              # Chat-only channel
    |       +-- CLAUDE.md
    |       +-- attachments/   # Downloaded Discord files (per-channel)
    |       +-- .claude/
    +-- shared/                # Shared resources
    |   +-- outbox/            # Message queue to Discord
    |   +-- wendy.db           # SQLite database
    +-- tmp/                   # Scratch space

Usage:
    from bot.paths import (
        WENDY_BASE, CHANNELS_DIR, SHARED_DIR,
        channel_dir, beads_dir, session_dir
    )

    # Get channel workspace path
    cwd = channel_dir("coding")  # /data/wendy/channels/coding

    # Get beads directory for a channel
    beads = beads_dir("coding")  # /data/wendy/channels/coding/.beads

    # Get Claude session directory for a channel
    sessions = session_dir("coding")  # /root/.claude/projects/-data-wendy-channels-coding
"""

from __future__ import annotations

import re
from pathlib import Path

# =============================================================================
# Base Paths
# =============================================================================

WENDY_BASE: Path = Path("/data/wendy")
"""Root directory for all Wendy data."""

CHANNELS_DIR: Path = WENDY_BASE / "channels"
"""Directory containing per-channel workspaces."""

SHARED_DIR: Path = WENDY_BASE / "shared"
"""Directory for shared resources (attachments, outbox, database)."""

TMP_DIR: Path = WENDY_BASE / "tmp"
"""Scratch space for temporary files."""

# =============================================================================
# Shared Resource Paths
# =============================================================================

OUTBOX_DIR: Path = SHARED_DIR / "outbox"
"""Directory where queued outgoing messages are written as JSON files."""

DB_PATH: Path = SHARED_DIR / "wendy.db"
"""Path to the SQLite database for message history and state."""

# =============================================================================
# Legacy Paths (for reference during migration)
# =============================================================================

LEGACY_DB_PATH: Path = Path("/data/wendy.db")
"""Old database location (at /data/wendy.db instead of /data/wendy/shared/wendy.db)."""

# =============================================================================
# Claude Session Paths
# =============================================================================

CLAUDE_PROJECTS_DIR: Path = Path("/root/.claude/projects")
"""Root directory where Claude CLI stores session files."""


def _encode_path_for_claude(path: Path) -> str:
    """Encode a path for Claude CLI project directory naming.

    Claude CLI encodes the working directory path by replacing '/' with '-'
    and stripping the leading '/'.

    Args:
        path: Absolute path to encode.

    Returns:
        Encoded path string (e.g., /data/wendy/channels/coding -> -data-wendy-channels-coding)
    """
    return str(path).replace("/", "-")


# =============================================================================
# Channel Path Functions
# =============================================================================

# Regex for validating channel names (folder-safe)
CHANNEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_channel_name(name: str) -> bool:
    """Validate that a channel name is safe for use as a folder name.

    Args:
        name: Channel name to validate.

    Returns:
        True if the name is valid, False otherwise.
    """
    if not name:
        return False
    return bool(CHANNEL_NAME_PATTERN.match(name))


def channel_dir(name: str) -> Path:
    """Get the workspace directory for a channel.

    Args:
        name: Channel name (from config, becomes folder name).

    Returns:
        Path to the channel's workspace directory.

    Example:
        >>> channel_dir("coding")
        Path('/data/wendy/channels/coding')
    """
    return CHANNELS_DIR / name


def beads_dir(channel_name: str) -> Path:
    """Get the beads task queue directory for a channel.

    Args:
        channel_name: Channel name.

    Returns:
        Path to the channel's .beads directory.

    Example:
        >>> beads_dir("coding")
        Path('/data/wendy/channels/coding/.beads')
    """
    return channel_dir(channel_name) / ".beads"


def session_dir(channel_name: str) -> Path:
    """Get the Claude CLI session directory for a channel.

    Claude CLI stores sessions based on the working directory path,
    encoded by replacing '/' with '-'.

    Args:
        channel_name: Channel name.

    Returns:
        Path to the Claude CLI projects directory for this channel.

    Example:
        >>> session_dir("coding")
        Path('/root/.claude/projects/-data-wendy-channels-coding')
    """
    channel_path = channel_dir(channel_name)
    encoded = _encode_path_for_claude(channel_path)
    return CLAUDE_PROJECTS_DIR / encoded


def current_session_file(channel_name: str) -> Path:
    """Get the path to the .current_session file for a channel.

    This file contains the current Claude CLI session ID, which the
    orchestrator uses for session forking.

    Args:
        channel_name: Channel name.

    Returns:
        Path to the .current_session file.

    Example:
        >>> current_session_file("coding")
        Path('/data/wendy/channels/coding/.current_session')
    """
    return channel_dir(channel_name) / ".current_session"


def claude_md_path(channel_name: str) -> Path:
    """Get the path to the CLAUDE.md personal notes file for a channel.

    Args:
        channel_name: Channel name.

    Returns:
        Path to the CLAUDE.md file.

    Example:
        >>> claude_md_path("coding")
        Path('/data/wendy/channels/coding/CLAUDE.md')
    """
    return channel_dir(channel_name) / "CLAUDE.md"


def attachments_dir(channel_name: str) -> Path:
    """Get the attachments directory for a channel.

    Each channel has its own attachments directory to ensure isolation -
    Claude working in one channel cannot see attachments from other channels.

    Args:
        channel_name: Channel name.

    Returns:
        Path to the channel's attachments directory.

    Example:
        >>> attachments_dir("coding")
        Path('/data/wendy/channels/coding/attachments')
    """
    return channel_dir(channel_name) / "attachments"


def journal_dir(channel_name: str) -> Path:
    """Get the journal directory for a channel.

    Each channel has its own journal directory for long-term memory entries.

    Args:
        channel_name: Channel name.

    Returns:
        Path to the channel's journal directory.

    Example:
        >>> journal_dir("coding")
        Path('/data/wendy/channels/coding/journal')
    """
    return channel_dir(channel_name) / "journal"


# =============================================================================
# Utility Functions
# =============================================================================


def ensure_channel_dirs(channel_name: str, beads_enabled: bool = False) -> None:
    """Ensure all directories exist for a channel.

    Creates the channel workspace directory, attachments directory, and
    optionally the .beads directory if beads_enabled is True.

    Args:
        channel_name: Channel name.
        beads_enabled: Whether to create the .beads directory.
    """
    channel_dir(channel_name).mkdir(parents=True, exist_ok=True)
    attachments_dir(channel_name).mkdir(exist_ok=True)
    journal_dir(channel_name).mkdir(exist_ok=True)

    if beads_enabled:
        beads_dir(channel_name).mkdir(exist_ok=True)


def ensure_shared_dirs() -> None:
    """Ensure all shared directories exist.

    Creates the shared/ and outbox/ directories.
    """
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
