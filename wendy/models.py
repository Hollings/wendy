"""Data structures for Wendy bot.

Leaf module -- zero internal imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ChannelConfig:
    """Parsed channel configuration from WENDY_CHANNEL_CONFIG."""

    id: int
    name: str
    mode: str = "chat"
    model: str | None = None
    beads_enabled: bool = False
    folder: str = ""  # defaults to name if empty

    # Thread-specific fields
    is_thread: bool = False
    parent_folder: str | None = None
    parent_channel_id: int | None = None
    thread_name: str | None = None

    def __post_init__(self):
        if not self.folder:
            self.folder = self.name


@dataclass(slots=True)
class SessionInfo:
    """Claude CLI session state for a channel."""

    channel_id: int
    session_id: str
    folder: str
    created_at: int
    last_used_at: int | None
    message_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_create_tokens: int


@dataclass(slots=True)
class Notification:
    """Unified notification (task completions, webhooks, etc.)."""

    id: int
    type: str
    source: str
    channel_id: int | None
    title: str
    payload: dict | None
    seen_by_wendy: bool
    seen_by_proxy: bool
    created_at: str


@dataclass(slots=True)
class ConversationMessage:
    """A Discord message for context building."""

    message_id: int
    author: str
    content: str
    timestamp: int | str
    attachments: list[str] = field(default_factory=list)
    reply_to_id: int | None = None
    reply_author: str | None = None
    reply_content: str | None = None
