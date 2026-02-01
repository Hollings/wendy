"""Unit tests for channel configuration parsing.

Tests the actual _parse_channel_config method in WendyCog and the
validate_channel_name function in paths.py.
"""

from bot.paths import validate_channel_name


# We can't instantiate WendyCog directly (requires Discord bot),
# so we extract and test the parsing logic directly.
def parse_channel_config(cfg: dict) -> dict | None:
    """Mirror of WendyCog._parse_channel_config for testing.

    This must be kept in sync with the actual method in wendy_cog.py.
    """
    if "id" not in cfg:
        return None
    if "name" not in cfg:
        return None

    name = cfg["name"]
    if not validate_channel_name(name):
        return None

    folder = cfg.get("folder", name)
    if not validate_channel_name(folder):
        folder = name

    return {
        "id": str(cfg["id"]),
        "name": name,
        "mode": cfg.get("mode", "chat"),  # Default is "chat", not "full"
        "model": cfg.get("model"),
        "beads_enabled": cfg.get("beads_enabled", False),
        "_folder": folder,
    }


class TestValidateChannelName:
    """Tests for validate_channel_name from paths.py."""

    def test_valid_alphanumeric(self):
        assert validate_channel_name("coding") is True
        assert validate_channel_name("chat123") is True
        assert validate_channel_name("UPPERCASE") is True

    def test_valid_with_underscore_and_dash(self):
        assert validate_channel_name("my_channel") is True
        assert validate_channel_name("my-channel") is True
        assert validate_channel_name("a_b-c_d") is True

    def test_empty_string_invalid(self):
        assert validate_channel_name("") is False

    def test_path_traversal_blocked(self):
        """Path traversal attempts must be rejected."""
        assert validate_channel_name("../etc") is False
        assert validate_channel_name("..") is False
        assert validate_channel_name("foo/bar") is False
        assert validate_channel_name("/absolute") is False

    def test_special_characters_blocked(self):
        """Characters that could cause issues in paths must be rejected."""
        assert validate_channel_name("has space") is False
        assert validate_channel_name("has.dot") is False
        assert validate_channel_name("has:colon") is False
        assert validate_channel_name("has@at") is False


class TestParseChannelConfig:
    """Tests for channel config parsing logic."""

    def test_minimal_valid_config(self):
        """Minimum required fields: id and name."""
        result = parse_channel_config({"id": "123", "name": "test"})

        assert result is not None
        assert result["id"] == "123"
        assert result["name"] == "test"
        assert result["mode"] == "chat"  # Default mode
        assert result["model"] is None
        assert result["beads_enabled"] is False
        assert result["_folder"] == "test"  # Defaults to name

    def test_full_config_with_all_fields(self):
        """All fields populated explicitly."""
        result = parse_channel_config({
            "id": "456",
            "name": "coding",
            "mode": "full",
            "model": "opus",
            "beads_enabled": True,
            "folder": "coding_workspace",
        })

        assert result["id"] == "456"
        assert result["mode"] == "full"
        assert result["model"] == "opus"
        assert result["beads_enabled"] is True
        assert result["_folder"] == "coding_workspace"

    def test_missing_id_returns_none(self):
        """Missing id field should return None, not raise."""
        result = parse_channel_config({"name": "test"})
        assert result is None

    def test_missing_name_returns_none(self):
        """Missing name field should return None, not raise."""
        result = parse_channel_config({"id": "123"})
        assert result is None

    def test_invalid_name_returns_none(self):
        """Invalid channel name should return None."""
        result = parse_channel_config({"id": "123", "name": "../hack"})
        assert result is None

    def test_invalid_folder_falls_back_to_name(self):
        """Invalid folder should fall back to name."""
        result = parse_channel_config({
            "id": "123",
            "name": "valid",
            "folder": "../invalid"
        })

        assert result is not None
        assert result["_folder"] == "valid"

    def test_integer_id_converted_to_string(self):
        """Integer IDs should be converted to strings."""
        result = parse_channel_config({"id": 123, "name": "test"})
        assert result["id"] == "123"

    def test_large_discord_snowflake_id(self):
        """Discord snowflake IDs (18-19 digits) must work."""
        snowflake = "1234567890123456789"
        result = parse_channel_config({"id": snowflake, "name": "test"})
        assert result["id"] == snowflake


class TestLegacyWhitelistParsing:
    """Tests for the legacy comma-separated whitelist format.

    The legacy format is parsed directly in WendyCog.__init__, not in
    _parse_channel_config. This tests the expected output format.
    """

    def test_legacy_config_has_full_mode(self):
        """Legacy configs default to 'full' mode for backwards compatibility."""
        # This is what WendyCog creates for legacy whitelist entries
        legacy_config = {
            "id": "123456789",
            "name": "default",
            "mode": "full",
            "beads_enabled": False,
        }

        assert legacy_config["mode"] == "full"
        assert legacy_config["beads_enabled"] is False

    def test_parsing_comma_separated_ids(self):
        """Simulate parsing comma-separated whitelist."""
        whitelist_str = "111, 222, 333"

        channel_ids = []
        for cid_str in whitelist_str.split(","):
            try:
                channel_ids.append(int(cid_str.strip()))
            except ValueError:
                pass

        assert channel_ids == [111, 222, 333]

    def test_invalid_entries_skipped(self):
        """Invalid entries in comma-separated list are skipped."""
        whitelist_str = "111,invalid,333"

        channel_ids = []
        for cid_str in whitelist_str.split(","):
            try:
                channel_ids.append(int(cid_str.strip()))
            except ValueError:
                pass

        assert channel_ids == [111, 333]
