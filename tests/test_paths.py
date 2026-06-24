"""Tests for wendy.paths -- pure path construction and helpers."""

from pathlib import Path

import pytest

from wendy import paths


@pytest.mark.parametrize(
    "name,expected",
    [
        ("chat", True),
        ("coding-123", True),
        ("my_channel", True),
        ("ABC_def-9", True),
        ("", False),
        ("has space", False),
        ("dots.bad", False),
        ("slash/bad", False),
        ("unicodé", False),
        ("special!", False),
    ],
)
def test_validate_channel_name(name, expected):
    assert paths.validate_channel_name(name) is expected


def test_encode_path_for_claude_replaces_slashes():
    assert paths._encode_path_for_claude(Path("/data/wendy/channels/chat")) == "-data-wendy-channels-chat"


def test_encode_path_for_claude_handles_relative():
    assert paths._encode_path_for_claude(Path("a/b/c")) == "a-b-c"


def test_path_builders_are_nested_under_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CHANNELS_DIR", tmp_path)
    base = paths.channel_dir("chat")
    assert base == tmp_path / "chat"
    assert paths.beads_dir("chat") == base / ".beads"
    assert paths.current_session_file("chat") == base / ".current_session"
    assert paths.claude_md_path("chat") == base / "CLAUDE.md"
    assert paths.attachments_dir("chat") == base / "attachments"
    assert paths.journal_dir("chat") == base / "journal"


def test_find_attachments_returns_empty_without_channel():
    assert paths.find_attachments_for_message(123, None) == []


def test_find_attachments_returns_empty_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CHANNELS_DIR", tmp_path)
    assert paths.find_attachments_for_message(123, "chat") == []


def test_find_attachments_matches_pattern_and_sorts(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CHANNELS_DIR", tmp_path)
    att = paths.attachments_dir("chat")
    att.mkdir(parents=True)
    (att / "msg_42_b.png").write_text("x")
    (att / "msg_42_a.png").write_text("x")
    (att / "msg_99_other.png").write_text("x")  # different message id

    result = paths.find_attachments_for_message(42, "chat")
    assert [Path(p).name for p in result] == ["msg_42_a.png", "msg_42_b.png"]
