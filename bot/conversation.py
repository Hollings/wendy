"""Conversation data structures for Wendy bot.

This module defines immutable data structures for representing conversation
history, including support for image attachments.

These dataclasses are used to:
- Store conversation turns from Discord messages
- Format payloads for LLM API requests
- Track message metadata (author, message ID, etc.)

All dataclasses use slots=True for memory efficiency.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ImageAttachment:
    """Represents an image attachment with metadata and encoded data.

    Used to pass images from Discord messages to the LLM API.

    Attributes:
        name: Original filename of the attachment.
        url: Discord CDN URL for the image.
        data_url: Base64-encoded data URL (data:image/...;base64,...).
        width: Image width in pixels (if known).
        height: Image height in pixels (if known).
        size: File size in bytes (if known).
    """

    name: str
    url: str
    data_url: str
    width: int | None = None
    height: int | None = None
    size: int | None = None

    def to_payload(self) -> dict[str, str]:
        """Convert to API payload format for LLM requests.

        Returns:
            Dict with 'data_url' key containing the base64 image.
        """
        return {"data_url": self.data_url}


@dataclass(slots=True)
class ConversationTurn:
    """Represents a single turn in the conversation history.

    Stores both the message content and Discord-specific metadata.

    Attributes:
        role: The role of the speaker ("user" or "assistant").
        content: The text content of the message.
        images: List of image attachments in this turn.
        message_id: Discord snowflake ID of the message.
        author_id: Discord user ID of the author.
        author_name: Display name of the author.
        webhook_id: Discord webhook ID if sent via webhook.
    """

    role: str
    content: str
    images: list[ImageAttachment] = field(default_factory=list)
    message_id: int | None = None
    author_id: int | None = None
    author_name: str | None = None
    webhook_id: int | None = None


@dataclass(slots=True)
class ModelTurn:
    """Lightweight turn for sending to LLM API.

    A simplified representation of ConversationTurn that only includes
    data needed for the LLM request.

    Attributes:
        role: The role of the speaker ("user" or "assistant").
        text: The text content of the message.
        images: List of image attachments in this turn.
    """

    role: str
    text: str
    images: list[ImageAttachment] = field(default_factory=list)
