"""Unit tests for channel configuration parsing."""

import json

import pytest


def parse_channel_config(config_json: str) -> tuple[dict[int, dict], set[int]]:
    """Parse WENDY_CHANNEL_CONFIG JSON into channel_configs and whitelist.

    Returns:
        Tuple of (channel_configs dict, whitelist_channels set)
    """
    channel_configs = {}
    whitelist_channels = set()

    if not config_json:
        return channel_configs, whitelist_channels

    configs = json.loads(config_json)
    for cfg in configs:
        channel_id = int(cfg["id"])
        channel_configs[channel_id] = cfg
        whitelist_channels.add(channel_id)

    return channel_configs, whitelist_channels


def parse_legacy_whitelist(whitelist_str: str) -> tuple[dict[int, dict], set[int]]:
    """Parse legacy WENDY_WHITELIST_CHANNELS comma-separated format.

    Returns:
        Tuple of (channel_configs dict, whitelist_channels set)
    """
    channel_configs = {}
    whitelist_channels = set()

    if not whitelist_str:
        return channel_configs, whitelist_channels

    for cid_str in whitelist_str.split(","):
        try:
            channel_id = int(cid_str.strip())
            whitelist_channels.add(channel_id)
            channel_configs[channel_id] = {
                "id": str(channel_id),
                "name": "default",
                "folder": "wendys_folder",
                "mode": "full"
            }
        except ValueError:
            pass

    return channel_configs, whitelist_channels


class TestParseChannelConfig:
    """Tests for JSON channel config parsing."""

    def test_single_channel(self):
        """Should parse single channel config."""
        config = '[{"id":"123","name":"chat","folder":"chat","mode":"chat"}]'
        configs, whitelist = parse_channel_config(config)

        assert 123 in whitelist
        assert configs[123]["name"] == "chat"
        assert configs[123]["folder"] == "chat"
        assert configs[123]["mode"] == "chat"

    def test_multiple_channels(self):
        """Should parse multiple channel configs."""
        config = '''[
            {"id":"111","name":"chat","folder":"chat","mode":"chat"},
            {"id":"222","name":"coding","folder":"coding","mode":"full"}
        ]'''
        configs, whitelist = parse_channel_config(config)

        assert len(whitelist) == 2
        assert 111 in whitelist
        assert 222 in whitelist
        assert configs[111]["mode"] == "chat"
        assert configs[222]["mode"] == "full"

    def test_empty_config(self):
        """Should return empty for empty string."""
        configs, whitelist = parse_channel_config("")
        assert len(configs) == 0
        assert len(whitelist) == 0

    def test_string_id_converted_to_int(self):
        """Should convert string IDs to integers."""
        config = '[{"id":"1234567890123456789","name":"test","folder":"test","mode":"full"}]'
        configs, whitelist = parse_channel_config(config)

        assert 1234567890123456789 in whitelist
        assert isinstance(list(whitelist)[0], int)

    def test_invalid_json_raises(self):
        """Should raise on invalid JSON."""
        with pytest.raises(json.JSONDecodeError):
            parse_channel_config("not valid json")

    def test_missing_id_raises(self):
        """Should raise on missing id field."""
        with pytest.raises(KeyError):
            parse_channel_config('[{"name":"test"}]')


class TestParseLegacyWhitelist:
    """Tests for legacy comma-separated whitelist parsing."""

    def test_single_channel(self):
        """Should parse single channel ID."""
        configs, whitelist = parse_legacy_whitelist("123456789")

        assert 123456789 in whitelist
        assert configs[123456789]["mode"] == "full"
        assert configs[123456789]["folder"] == "wendys_folder"

    def test_multiple_channels(self):
        """Should parse comma-separated channel IDs."""
        configs, whitelist = parse_legacy_whitelist("111,222,333")

        assert len(whitelist) == 3
        assert 111 in whitelist
        assert 222 in whitelist
        assert 333 in whitelist

    def test_whitespace_handling(self):
        """Should handle whitespace around IDs."""
        configs, whitelist = parse_legacy_whitelist("111 , 222 , 333")

        assert len(whitelist) == 3
        assert 111 in whitelist

    def test_empty_string(self):
        """Should return empty for empty string."""
        configs, whitelist = parse_legacy_whitelist("")
        assert len(configs) == 0
        assert len(whitelist) == 0

    def test_invalid_id_skipped(self):
        """Should skip invalid channel IDs."""
        configs, whitelist = parse_legacy_whitelist("123,invalid,456")

        assert len(whitelist) == 2
        assert 123 in whitelist
        assert 456 in whitelist

    def test_default_config_values(self):
        """Should create default config for each channel."""
        configs, whitelist = parse_legacy_whitelist("123")

        assert configs[123]["name"] == "default"
        assert configs[123]["folder"] == "wendys_folder"
        assert configs[123]["mode"] == "full"
        assert configs[123]["id"] == "123"


class TestChannelModes:
    """Tests for channel mode behavior."""

    def test_chat_mode_config(self):
        """Chat mode should have restricted settings."""
        config = '[{"id":"123","name":"chat","folder":"chat","mode":"chat"}]'
        configs, _ = parse_channel_config(config)

        assert configs[123]["mode"] == "chat"
        # Chat mode uses its own folder
        assert configs[123]["folder"] == "chat"

    def test_full_mode_config(self):
        """Full mode should have full access settings."""
        config = '[{"id":"123","name":"coding","folder":"coding","mode":"full"}]'
        configs, _ = parse_channel_config(config)

        assert configs[123]["mode"] == "full"

    def test_find_full_mode_channel(self):
        """Should be able to find a full mode channel."""
        config = '''[
            {"id":"111","name":"chat","folder":"chat","mode":"chat"},
            {"id":"222","name":"coding","folder":"coding","mode":"full"}
        ]'''
        configs, _ = parse_channel_config(config)

        # Find first full mode channel (for task completions)
        full_mode_channel = None
        for cid, cfg in configs.items():
            if cfg.get("mode") == "full":
                full_mode_channel = cid
                break

        assert full_mode_channel == 222
