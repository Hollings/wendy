"""Tests for wendy.models dataclasses."""

from wendy.models import ChannelConfig


def test_channel_config_folder_defaults_to_name():
    cfg = ChannelConfig(id=1, name="coding")
    assert cfg.folder == "coding"


def test_channel_config_preserves_explicit_folder():
    cfg = ChannelConfig(id=1, name="coding", folder="custom")
    assert cfg.folder == "custom"


def test_channel_config_defaults():
    cfg = ChannelConfig(id=42, name="chat")
    assert cfg.mode == "chat"
    assert cfg.model is None
    assert cfg.beads_enabled is False
    assert cfg.is_thread is False
    assert cfg.parent_channel_id is None
