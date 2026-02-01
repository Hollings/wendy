"""Unit tests for orchestrator/main.py functions."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.main import Orchestrator


@pytest.fixture
def mock_orchestrator_init():
    """Mock filesystem operations during Orchestrator init."""
    with patch("orchestrator.main.LOG_DIR") as mock_log_dir:
        mock_log_dir.mkdir = MagicMock()
        yield


class TestParseModelFromLabels:
    """Tests for Orchestrator.parse_model_from_labels."""

    @pytest.fixture
    def orchestrator(self, mock_orchestrator_init):
        """Create orchestrator with mocked channel config."""
        with patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": "[]"}):
            return Orchestrator()

    def test_extract_opus(self, orchestrator):
        """Should extract opus model from labels."""
        labels = ["priority:high", "model:opus", "bug"]
        assert orchestrator.parse_model_from_labels(labels) == "opus"

    def test_extract_sonnet(self, orchestrator):
        """Should extract sonnet model from labels."""
        labels = ["model:sonnet"]
        assert orchestrator.parse_model_from_labels(labels) == "sonnet"

    def test_no_model_label(self, orchestrator):
        """Should return None when no model label present."""
        labels = ["bug", "priority:high", "feature"]
        assert orchestrator.parse_model_from_labels(labels) is None

    def test_empty_labels(self, orchestrator):
        """Should return None for empty labels."""
        assert orchestrator.parse_model_from_labels([]) is None

    def test_none_labels(self, orchestrator):
        """Should return None for None labels."""
        assert orchestrator.parse_model_from_labels(None) is None

    def test_first_model_wins(self, orchestrator):
        """Should return first model if multiple present."""
        labels = ["model:opus", "model:sonnet"]
        assert orchestrator.parse_model_from_labels(labels) == "opus"

    def test_model_with_version(self, orchestrator):
        """Should handle model with version string."""
        labels = ["model:claude-3-opus-20240229"]
        assert orchestrator.parse_model_from_labels(labels) == "claude-3-opus-20240229"


class TestFormatResetTimePacific:
    """Tests for Orchestrator.format_reset_time_pacific."""

    @pytest.fixture
    def orchestrator(self, mock_orchestrator_init):
        """Create orchestrator with mocked channel config."""
        with patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": "[]"}):
            return Orchestrator()

    def test_empty_string(self, orchestrator):
        """Should return empty string for empty input."""
        assert orchestrator.format_reset_time_pacific("") == ""

    def test_utc_timestamp_converts_to_pacific(self, orchestrator):
        """Should convert UTC timestamp to Pacific time with correct offset."""
        # 2024-01-15 20:00:00 UTC = 2024-01-15 12:00:00 PST (UTC-8)
        result = orchestrator.format_reset_time_pacific("2024-01-15T20:00:00Z")
        assert result == "Mon Jan 15, 12:00PM PT"

    def test_utc_timestamp_with_offset(self, orchestrator):
        """Should handle timestamp with explicit offset."""
        # Noon UTC should become 4AM Pacific
        result = orchestrator.format_reset_time_pacific("2024-01-15T12:00:00+00:00")
        assert result == "Mon Jan 15, 04:00AM PT"

    def test_invalid_timestamp_returns_original(self, orchestrator):
        """Should return original string for invalid timestamp."""
        result = orchestrator.format_reset_time_pacific("not-a-timestamp")
        assert result == "not-a-timestamp"


class TestLoadBeadsChannels:
    """Tests for Orchestrator._load_beads_channels config parsing."""

    def test_empty_config_returns_empty_list(self, mock_orchestrator_init):
        """Should return empty list when WENDY_CHANNEL_CONFIG is not set."""
        with patch.dict(os.environ, {}, clear=True):
            orchestrator = Orchestrator()
            assert orchestrator.beads_channels == []

    def test_no_beads_enabled_channels(self, mock_orchestrator_init):
        """Should return empty list when no channels have beads_enabled."""
        config = json.dumps([
            {"id": "123", "name": "chat", "folder": "chat", "mode": "chat"},
            {"id": "456", "name": "general", "folder": "general", "mode": "full"}
        ])
        with patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": config}):
            orchestrator = Orchestrator()
            assert orchestrator.beads_channels == []

    def test_extracts_beads_enabled_channels(self, mock_orchestrator_init):
        """Should extract channels with beads_enabled: true."""
        config = json.dumps([
            {"id": "123", "name": "chat", "folder": "chat", "mode": "chat"},
            {"id": "456", "name": "coding", "folder": "coding", "mode": "full", "beads_enabled": True},
            {"id": "789", "name": "dev", "folder": "dev", "mode": "full", "beads_enabled": True}
        ])
        with patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": config}):
            orchestrator = Orchestrator()
            channel_names = [c.name for c in orchestrator.beads_channels]
            assert channel_names == ["coding", "dev"]

    def test_uses_folder_over_name_when_present(self, mock_orchestrator_init):
        """Should prefer 'folder' field over 'name' for channel name."""
        config = json.dumps([
            {"id": "123", "name": "display-name", "folder": "actual-folder", "beads_enabled": True}
        ])
        with patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": config}):
            orchestrator = Orchestrator()
            assert orchestrator.beads_channels[0].name == "actual-folder"

    def test_falls_back_to_name_when_no_folder(self, mock_orchestrator_init):
        """Should use 'name' when 'folder' is not present."""
        config = json.dumps([
            {"id": "123", "name": "channel-name", "beads_enabled": True}
        ])
        with patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": config}):
            orchestrator = Orchestrator()
            assert orchestrator.beads_channels[0].name == "channel-name"

    def test_invalid_json_returns_empty_list(self, mock_orchestrator_init):
        """Should return empty list for invalid JSON config."""
        with patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": "not valid json"}):
            orchestrator = Orchestrator()
            assert orchestrator.beads_channels == []

    def test_skips_channels_without_name_or_folder(self, mock_orchestrator_init):
        """Should skip channel configs that have no name or folder."""
        config = json.dumps([
            {"id": "123", "beads_enabled": True},  # No name or folder
            {"id": "456", "name": "valid", "beads_enabled": True}
        ])
        with patch.dict(os.environ, {"WENDY_CHANNEL_CONFIG": config}):
            orchestrator = Orchestrator()
            assert len(orchestrator.beads_channels) == 1
            assert orchestrator.beads_channels[0].name == "valid"
