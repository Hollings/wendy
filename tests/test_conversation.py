"""Unit tests for bot/conversation.py dataclasses.

These dataclasses are simple data containers. We only test:
1. Actual logic (to_payload method)
2. Default factory behavior (lists shouldn't share state)
3. slots=True enforcement
"""

from bot.conversation import ConversationTurn, ImageAttachment, ModelTurn


class TestImageAttachment:
    """Tests for ImageAttachment dataclass."""

    def test_to_payload_returns_only_data_url(self):
        """to_payload should return dict with only data_url key."""
        attachment = ImageAttachment(
            name="test.png",
            url="https://example.com/test.png",
            data_url="data:image/png;base64,abc123",
            width=800,
            height=600,
            size=12345
        )
        payload = attachment.to_payload()
        assert payload == {"data_url": "data:image/png;base64,abc123"}
        assert len(payload) == 1

    def test_slots_prevents_arbitrary_attributes(self):
        """slots=True should prevent adding arbitrary attributes."""
        attachment = ImageAttachment(
            name="test.png",
            url="https://example.com/test.png",
            data_url="data:image/png;base64,abc123"
        )
        try:
            attachment.arbitrary_field = "should fail"
            assert False, "Expected AttributeError for slots class"
        except AttributeError:
            pass


class TestConversationTurn:
    """Tests for ConversationTurn dataclass."""

    def test_images_default_factory_not_shared(self):
        """Each instance should get its own empty list, not a shared one."""
        turn1 = ConversationTurn(role="user", content="Hello")
        turn2 = ConversationTurn(role="user", content="World")

        # Mutate turn1's images list
        turn1.images.append(
            ImageAttachment(name="x.png", url="x", data_url="data:x")
        )

        # turn2's images should be unaffected
        assert turn1.images != turn2.images
        assert len(turn2.images) == 0

    def test_slots_prevents_arbitrary_attributes(self):
        """slots=True should prevent adding arbitrary attributes."""
        turn = ConversationTurn(role="user", content="test")
        try:
            turn.extra_field = "should fail"
            assert False, "Expected AttributeError for slots class"
        except AttributeError:
            pass


class TestModelTurn:
    """Tests for ModelTurn dataclass."""

    def test_images_default_factory_not_shared(self):
        """Each instance should get its own empty list, not a shared one."""
        turn1 = ModelTurn(role="user", text="Hello")
        turn2 = ModelTurn(role="user", text="World")

        # Mutate turn1's images list
        turn1.images.append(
            ImageAttachment(name="x.png", url="x", data_url="data:x")
        )

        # turn2's images should be unaffected
        assert turn1.images != turn2.images
        assert len(turn2.images) == 0

    def test_slots_prevents_arbitrary_attributes(self):
        """slots=True should prevent adding arbitrary attributes."""
        turn = ModelTurn(role="user", text="test")
        try:
            turn.unexpected = "should fail"
            assert False, "Expected AttributeError for slots class"
        except AttributeError:
            pass
