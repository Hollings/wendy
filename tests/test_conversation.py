"""Unit tests for bot/conversation.py dataclasses."""

import pytest
from bot.conversation import ImageAttachment, ConversationTurn, ModelTurn


class TestImageAttachment:
    """Tests for ImageAttachment dataclass."""

    def test_create_basic_attachment(self):
        """Should create attachment with required fields."""
        attachment = ImageAttachment(
            name="test.png",
            url="https://example.com/test.png",
            data_url="data:image/png;base64,abc123"
        )
        assert attachment.name == "test.png"
        assert attachment.url == "https://example.com/test.png"
        assert attachment.data_url == "data:image/png;base64,abc123"

    def test_optional_dimensions(self):
        """Should support optional width/height."""
        attachment = ImageAttachment(
            name="test.png",
            url="https://example.com/test.png",
            data_url="data:image/png;base64,abc123",
            width=800,
            height=600
        )
        assert attachment.width == 800
        assert attachment.height == 600

    def test_optional_size(self):
        """Should support optional file size."""
        attachment = ImageAttachment(
            name="test.png",
            url="https://example.com/test.png",
            data_url="data:image/png;base64,abc123",
            size=12345
        )
        assert attachment.size == 12345

    def test_defaults_are_none(self):
        """Optional fields should default to None."""
        attachment = ImageAttachment(
            name="test.png",
            url="https://example.com/test.png",
            data_url="data:image/png;base64,abc123"
        )
        assert attachment.width is None
        assert attachment.height is None
        assert attachment.size is None

    def test_to_payload(self):
        """Should convert to API payload format."""
        attachment = ImageAttachment(
            name="test.png",
            url="https://example.com/test.png",
            data_url="data:image/png;base64,abc123"
        )
        payload = attachment.to_payload()
        assert payload == {"data_url": "data:image/png;base64,abc123"}

    def test_to_payload_only_includes_data_url(self):
        """Payload should only contain data_url, not other fields."""
        attachment = ImageAttachment(
            name="test.png",
            url="https://example.com/test.png",
            data_url="data:image/png;base64,abc123",
            width=800,
            height=600,
            size=12345
        )
        payload = attachment.to_payload()
        assert list(payload.keys()) == ["data_url"]


class TestConversationTurn:
    """Tests for ConversationTurn dataclass."""

    def test_create_user_turn(self):
        """Should create user turn."""
        turn = ConversationTurn(role="user", content="Hello!")
        assert turn.role == "user"
        assert turn.content == "Hello!"
        assert turn.images == []

    def test_create_assistant_turn(self):
        """Should create assistant turn."""
        turn = ConversationTurn(role="assistant", content="Hi there!")
        assert turn.role == "assistant"
        assert turn.content == "Hi there!"

    def test_turn_with_images(self):
        """Should support image attachments."""
        img = ImageAttachment(
            name="photo.jpg",
            url="https://example.com/photo.jpg",
            data_url="data:image/jpeg;base64,xyz"
        )
        turn = ConversationTurn(role="user", content="Look at this", images=[img])
        assert len(turn.images) == 1
        assert turn.images[0].name == "photo.jpg"

    def test_turn_with_metadata(self):
        """Should support Discord metadata."""
        turn = ConversationTurn(
            role="user",
            content="Hello",
            message_id=123456789,
            author_id=987654321,
            author_name="TestUser"
        )
        assert turn.message_id == 123456789
        assert turn.author_id == 987654321
        assert turn.author_name == "TestUser"

    def test_webhook_id(self):
        """Should support webhook ID for bot messages."""
        turn = ConversationTurn(
            role="assistant",
            content="Response",
            webhook_id=111222333
        )
        assert turn.webhook_id == 111222333

    def test_defaults(self):
        """Optional fields should have proper defaults."""
        turn = ConversationTurn(role="user", content="test")
        assert turn.images == []
        assert turn.message_id is None
        assert turn.author_id is None
        assert turn.author_name is None
        assert turn.webhook_id is None


class TestModelTurn:
    """Tests for ModelTurn dataclass."""

    def test_create_basic_turn(self):
        """Should create basic turn for LLM."""
        turn = ModelTurn(role="user", text="What is Python?")
        assert turn.role == "user"
        assert turn.text == "What is Python?"
        assert turn.images == []

    def test_turn_with_images(self):
        """Should support images for vision models."""
        img = ImageAttachment(
            name="code.png",
            url="https://example.com/code.png",
            data_url="data:image/png;base64,abc"
        )
        turn = ModelTurn(role="user", text="Explain this code", images=[img])
        assert len(turn.images) == 1

    def test_empty_text(self):
        """Should allow empty text (for image-only messages)."""
        img = ImageAttachment(
            name="img.png",
            url="https://example.com/img.png",
            data_url="data:image/png;base64,abc"
        )
        turn = ModelTurn(role="user", text="", images=[img])
        assert turn.text == ""
        assert len(turn.images) == 1
