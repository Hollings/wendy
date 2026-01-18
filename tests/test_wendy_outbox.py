"""Unit tests for bot/wendy_outbox.py functions."""

import re
import pytest


def extract_outbox_timestamp(filename: str) -> int | None:
    """Extract timestamp from outbox filename like '123456_1234567890123.json'.

    This is a standalone version of the method for testing without Discord deps.
    """
    match = re.match(r"\d+_(\d+)\.json$", filename)
    if match:
        return int(match.group(1))
    return None


class TestExtractOutboxTimestamp:
    """Tests for outbox timestamp extraction."""

    def test_valid_filename(self):
        """Should extract timestamp from valid filename."""
        result = extract_outbox_timestamp("123456_1705123456789.json")
        assert result == 1705123456789

    def test_different_channel_id(self):
        """Should work with different channel ID lengths."""
        result = extract_outbox_timestamp("999999999999999999_1705123456789.json")
        assert result == 1705123456789

    def test_invalid_extension(self):
        """Should return None for non-json files."""
        result = extract_outbox_timestamp("123456_1705123456789.txt")
        assert result is None

    def test_missing_underscore(self):
        """Should return None for filenames without underscore."""
        result = extract_outbox_timestamp("1705123456789.json")
        assert result is None

    def test_non_numeric_parts(self):
        """Should return None for non-numeric parts."""
        result = extract_outbox_timestamp("abc_def.json")
        assert result is None

    def test_empty_string(self):
        """Should return None for empty string."""
        result = extract_outbox_timestamp("")
        assert result is None

    def test_partial_match(self):
        """Should not match partial patterns."""
        result = extract_outbox_timestamp("prefix_123456_1705123456789.json")
        assert result is None
