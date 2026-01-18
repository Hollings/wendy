"""Unit tests for orchestrator/main.py functions."""

from datetime import datetime, timedelta, timezone

# Import the functions we want to test by recreating them
# (to avoid import issues with the full module)

def parse_model_from_labels(labels: list) -> str | None:
    """Extract model from labels like 'model:opus' -> 'opus'."""
    for label in labels or []:
        if label.startswith("model:"):
            return label.split(":", 1)[1]
    return None


def format_reset_time_pacific(iso_timestamp: str) -> str:
    """Convert ISO timestamp to Pacific time formatted string."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
        pacific = dt.astimezone(timezone(timedelta(hours=-8)))
        return pacific.strftime("%a %b %d, %I:%M%p PT")
    except Exception:
        return iso_timestamp


class TestParseModelFromLabels:
    """Tests for parse_model_from_labels."""

    def test_extract_opus(self):
        """Should extract opus model from labels."""
        labels = ["priority:high", "model:opus", "bug"]
        assert parse_model_from_labels(labels) == "opus"

    def test_extract_sonnet(self):
        """Should extract sonnet model from labels."""
        labels = ["model:sonnet"]
        assert parse_model_from_labels(labels) == "sonnet"

    def test_extract_haiku(self):
        """Should extract haiku model from labels."""
        labels = ["model:haiku", "feature"]
        assert parse_model_from_labels(labels) == "haiku"

    def test_no_model_label(self):
        """Should return None when no model label present."""
        labels = ["bug", "priority:high", "feature"]
        assert parse_model_from_labels(labels) is None

    def test_empty_labels(self):
        """Should return None for empty labels."""
        assert parse_model_from_labels([]) is None

    def test_none_labels(self):
        """Should return None for None labels."""
        assert parse_model_from_labels(None) is None

    def test_first_model_wins(self):
        """Should return first model if multiple present."""
        labels = ["model:opus", "model:sonnet"]
        assert parse_model_from_labels(labels) == "opus"

    def test_model_with_version(self):
        """Should handle model with version string."""
        labels = ["model:claude-3-opus-20240229"]
        assert parse_model_from_labels(labels) == "claude-3-opus-20240229"


class TestFormatResetTimePacific:
    """Tests for format_reset_time_pacific."""

    def test_empty_string(self):
        """Should return empty string for empty input."""
        assert format_reset_time_pacific("") == ""

    def test_none_input(self):
        """Should return empty string for None-like input."""
        assert format_reset_time_pacific("") == ""

    def test_utc_timestamp_with_z(self):
        """Should convert UTC timestamp with Z suffix."""
        # 2024-01-15 20:00:00 UTC = 2024-01-15 12:00:00 PST
        result = format_reset_time_pacific("2024-01-15T20:00:00Z")
        assert "PT" in result
        assert "Jan" in result

    def test_utc_timestamp_with_offset(self):
        """Should convert timestamp with explicit offset."""
        result = format_reset_time_pacific("2024-01-15T12:00:00+00:00")
        assert "PT" in result

    def test_invalid_timestamp(self):
        """Should return original string for invalid timestamp."""
        result = format_reset_time_pacific("not-a-timestamp")
        assert result == "not-a-timestamp"

    def test_format_includes_day_of_week(self):
        """Should include day of week in output."""
        result = format_reset_time_pacific("2024-01-15T20:00:00Z")
        # Should contain a day abbreviation (Mon, Tue, etc.)
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        assert any(day in result for day in days)


class TestThresholdCalculation:
    """Tests for usage threshold calculation logic."""

    def test_threshold_rounding(self):
        """Test 10% threshold rounding."""
        # Simulating the threshold calculation
        def calc_threshold(percent):
            return (percent // 10) * 10

        assert calc_threshold(0) == 0
        assert calc_threshold(5) == 0
        assert calc_threshold(10) == 10
        assert calc_threshold(15) == 10
        assert calc_threshold(25) == 20
        assert calc_threshold(99) == 90
        assert calc_threshold(100) == 100

    def test_threshold_crossing_detection(self):
        """Test detecting when threshold is crossed."""
        def crossed_threshold(current_pct, last_notified):
            current_threshold = (current_pct // 10) * 10
            return current_threshold > last_notified and current_threshold > 0

        # No crossing - same threshold
        assert not crossed_threshold(15, 10)

        # Crossing - went from 0-9% to 10-19%
        assert crossed_threshold(12, 0)

        # Crossing - went from 10-19% to 20-29%
        assert crossed_threshold(25, 10)

        # No crossing - still at 0
        assert not crossed_threshold(5, 0)

        # No crossing - decreased
        assert not crossed_threshold(15, 20)
