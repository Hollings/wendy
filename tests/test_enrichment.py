"""Tests for wendy.enrichment nudge builders."""

from wendy.enrichment import (
    build_enrichment_continue_nudge,
    build_enrichment_end_nudge,
    build_enrichment_nudge,
)


def test_opening_nudge_embeds_end_time():
    nudge = build_enrichment_nudge("14:30")
    assert "14:30 UTC" in nudge
    assert nudge.startswith("<")
    assert nudge.endswith(">")


def test_opening_nudge_blocks_discord_and_config_edits():
    nudge = build_enrichment_nudge("09:00")
    # The nudge must clearly forbid Discord access and self-config edits.
    assert "send_message and check_messages are BLOCKED" in nudge
    assert "DO NOT edit your own configuration" in nudge


def test_continue_nudge_embeds_end_time():
    nudge = build_enrichment_continue_nudge("23:15")
    assert "23:15 UTC" in nudge
    assert "blocked" in nudge.lower()
    assert nudge.startswith("<") and nudge.endswith(">")


def test_end_nudge_restores_discord():
    nudge = build_enrichment_end_nudge()
    assert "Discord access is restored" in nudge
    assert nudge.startswith("<") and nudge.endswith(">")


def test_nudges_are_nonempty_strings():
    for fn_result in (
        build_enrichment_nudge("00:00"),
        build_enrichment_continue_nudge("00:00"),
        build_enrichment_end_nudge(),
    ):
        assert isinstance(fn_result, str)
        assert fn_result.strip()
