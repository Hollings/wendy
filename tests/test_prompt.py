"""Tests for wendy.prompt."""
from __future__ import annotations

import json
from unittest import mock

from wendy.prompt import (
    _get_base_system_prompt,
    _get_journal_section,
    build_system_prompt,
    get_beads_warning_for_nudge,
    get_journal_listing_for_nudge,
)


def test_get_base_system_prompt_missing_file():
    with mock.patch.dict("os.environ", {"SYSTEM_PROMPT_FILE": "/nonexistent/file.txt"}):
        result = _get_base_system_prompt("general")
        assert result == ""


def test_get_base_system_prompt_replaces_folder(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Welcome to {folder} channel.")
    with mock.patch.dict("os.environ", {"SYSTEM_PROMPT_FILE": str(prompt_file)}):
        result = _get_base_system_prompt("coding")
        assert "Welcome to coding channel." in result


def test_get_base_system_prompt_chat_mode_filters(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(
        "Header\n"
        "<!-- FULL_ONLY_START -->\n"
        "This section should be removed\n"
        "<!-- FULL_ONLY_END -->\n"
        "This should remain\n"
        "<!-- FULL_ONLY_START -->\n"
        "This should also be removed\n"
        "<!-- FULL_ONLY_END -->\n"
    )
    with mock.patch.dict("os.environ", {"SYSTEM_PROMPT_FILE": str(prompt_file)}):
        result = _get_base_system_prompt("chat", mode="chat")
        assert "Header" in result
        assert "This section should be removed" not in result
        assert "This should remain" in result
        assert "This should also be removed" not in result


def test_get_journal_section(tmp_path):
    j_dir = tmp_path / "journal"
    j_dir.mkdir()

    with mock.patch("wendy.prompt.journal_dir", return_value=j_dir):
        result = _get_journal_section("general")

    # Static section always contains journal header and path
    assert "JOURNAL" in result
    assert str(j_dir) in result


def test_get_journal_listing_for_nudge(tmp_path):
    j_dir = tmp_path / "journal"
    j_dir.mkdir()

    (j_dir / "2026-01-01_test.md").write_text("entry 1")
    (j_dir / "2026-01-02_test2.md").write_text("entry 2")

    with mock.patch("wendy.prompt.journal_dir", return_value=j_dir):
        result = get_journal_listing_for_nudge("general")

    assert "2026-01-01_test.md" in result
    assert "2026-01-02_test2.md" in result
    assert "2 files" in result


def test_get_journal_listing_for_nudge_empty(tmp_path):
    j_dir = tmp_path / "journal"
    j_dir.mkdir()

    with mock.patch("wendy.prompt.journal_dir", return_value=j_dir):
        result = get_journal_listing_for_nudge("general")

    assert result == ""


def test_get_beads_warning_for_nudge_no_beads(tmp_path):
    b_dir = tmp_path / ".beads"
    b_dir.mkdir()

    with mock.patch("wendy.prompt.beads_dir", return_value=b_dir):
        result = get_beads_warning_for_nudge("general")
    assert result == ""


def test_get_beads_warning_for_nudge_with_active_tasks(tmp_path):
    b_dir = tmp_path / ".beads"
    b_dir.mkdir()

    issues_file = b_dir / "issues.jsonl"
    issues = [
        {"id": "task-1", "title": "Do something", "status": "in_progress"},
        {"id": "task-2", "title": "Done thing", "status": "closed"},
        {"id": "task-3", "title": "Another task", "status": "in_progress"},
    ]
    issues_file.write_text("\n".join(json.dumps(i) for i in issues))

    with mock.patch("wendy.prompt.beads_dir", return_value=b_dir):
        result = get_beads_warning_for_nudge("general")

    assert "2 active bead(s)" in result
    assert "task-1" in result
    assert "task-3" in result
    assert "task-2" not in result  # closed task should not appear


def test_build_system_prompt_integration(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Base prompt for {folder}.")
    j_dir = tmp_path / "journal"
    j_dir.mkdir()

    channel_config = {
        "name": "coding",
        "_folder": "coding",
        "mode": "full",
        "beads_enabled": False,
    }

    with mock.patch.dict("os.environ", {"SYSTEM_PROMPT_FILE": str(prompt_file)}):
        with mock.patch("wendy.prompt.journal_dir", return_value=j_dir):
            with mock.patch("wendy.prompt.load_fragments", return_value={
                "persons": "",  # persons now injected via synthetic messages
                "channel": "\n--- CHANNEL ---\nChannel info\n",
                "topics": "\n--- TOPICS ---\nTopic info\n",
                "anchors": "\n--- ANCHORS ---\nAnchor info\n",
            }):
                with mock.patch("wendy.prompt.get_recent_messages", return_value=[]):
                    result = build_system_prompt(123, channel_config)

    assert "Base prompt for coding." in result
    assert "Person info" not in result  # persons no longer in system prompt
    assert "Channel info" in result
    assert "Topic info" in result
    assert "Anchor info" in result
    assert "JOURNAL" in result


def test_build_system_prompt_thread(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Base prompt for {folder}.")
    j_dir = tmp_path / "journal"
    j_dir.mkdir()

    channel_config = {
        "name": "thread",
        "_folder": "coding_t_999",
        "mode": "full",
        "beads_enabled": False,
        "_is_thread": True,
        "_parent_folder": "coding",
        "_thread_name": "my-thread",
        "_parent_channel_id": "456",
    }

    with mock.patch.dict("os.environ", {"SYSTEM_PROMPT_FILE": str(prompt_file)}):
        with mock.patch("wendy.prompt.journal_dir", return_value=j_dir):
            with mock.patch("wendy.prompt.load_fragments", return_value={
                "persons": "", "channel": "", "topics": "", "anchors": "",
            }):
                with mock.patch("wendy.prompt.get_recent_messages", return_value=[]):
                    result = build_system_prompt(123, channel_config)

    assert "THREAD CONTEXT" in result
    assert "my-thread" in result
